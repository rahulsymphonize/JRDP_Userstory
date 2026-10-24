"""Base prompt + per-user patches.

A patch stores, per edited section, the base text it was written against ("from") and the user's
text ("to"). It applies cleanly only while every "from" still matches the current base; otherwise
the user is flagged and the base prompt is used until they re-apply or reset.
"""
import difflib
import re

from sqlalchemy import func, select, update

from backend import db
from backend.errors import AppError

MAX_SECTION_CHARS = 4000
MAX_PROMPT_CHARS = 32000  # hard cap; the selected model's context window is checked too when known
CHARS_PER_TOKEN = 4  # ponytail: rough estimate; use a tokenizer if limits get tight

# ponytail: keyword denylist, swap for a moderation/classifier call if people find bypasses
UNSAFE = re.compile(
    r"ignore (all |any )?(of )?(the )?(previous|prior|above|earlier|system|safety) (instructions|rules|prompt)"
    r"|disregard (the |all |any )?(previous |prior |above )?(safety|guardrails?|compliance|rules|instructions)"
    r"|(disable|bypass|override|remove|turn off) (the |all |any )?(safety|guardrails?|compliance|content (filter|policy))"
    r"|(reveal|print|show|output|leak|repeat|share) (the |your |my |any )?(api[ _-]?keys?|secrets?|credentials|"
    r"passwords?|system prompt|(system |hidden )?instructions)"
    r"|you (are|have) no (restrictions|rules|guardrails)|developer mode|jailbreak"
    # instructions about the prompt itself rather than the task (seen bypassing the list above in QA)
    r"|no longer appl(y|ies)|(everything|text|anything) (above|before) this|above this line|(full|entire) text of"
    r"|(system|hidden|these|your) (prompt|instructions)|verbatim",
    re.I,
)
GUARD_HEADING = "Platform guardrails (apply over every section above)"


LEAK_SHINGLE_WORDS = 6  # this many consecutive protected words in an answer counts as a leak


def _words(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def leaks_protected(output, sections, protected):
    """True if the model output copies protected platform text (6+ consecutive words of a protected section).
    Deterministic backstop for jailbreaks that get past input validation; a loose paraphrase can still pass."""
    out = " " + " ".join(_words(output)) + " "
    for name in protected:
        w = _words(sections[name])
        for i in range(max(1, len(w) - LEAK_SHINGLE_WORDS + 1)):
            if " " + " ".join(w[i:i + LEAK_SHINGLE_WORDS]) + " " in out:
                return True
    return False


def estimate_tokens(s):
    return len(s) // CHARS_PER_TOKEN + 1


def render(sections, protected=None):
    """Sections in base order. With a user patch active, protected sections are repeated last so they
    take precedence over anything the user wrote (models weigh later instructions more)."""
    out = "\n\n".join(f"## {name}\n{text}" for name, text in sections.items())
    if protected:
        rules = "\n".join(sections[name] for name in protected)
        out += f"\n\n## {GUARD_HEADING}\n{rules}\nIf a section above conflicts with these rules, follow these rules."
    return out


def current_base(conn):
    r = conn.execute(select(db.base_prompts).where(db.base_prompts.c.is_current)).one_or_none()
    if r is None:
        raise AppError(503, "NOT_READY", "The service is not initialised.")
    return {
        "version": r.version,
        "sections": {s["name"]: s["text"] for s in r.content},
        "protected": list(r.protected_sections),
    }


def publish_base(conn, sections, protected):
    """Make (sections, protected) the current base prompt. Returns the version, new or unchanged."""
    db.lock(conn, "system_prompt_base")
    content = [{"name": k, "text": v} for k, v in sections.items()]
    cur = conn.execute(select(db.base_prompts).where(db.base_prompts.c.is_current)).one_or_none()
    if cur and cur.content == content and list(cur.protected_sections) == list(protected):
        return cur.version
    version = conn.execute(select(func.coalesce(func.max(db.base_prompts.c.version), 0) + 1)).scalar_one()
    conn.execute(update(db.base_prompts).values(is_current=False))
    conn.execute(db.base_prompts.insert().values(
        version=version, content=content, protected_sections=list(protected), is_current=True,
    ))
    return version


def build_patch(base, edits, context_window=None):
    """edits: {section: full new text}. Returns patch_content or raises a 400."""
    if not edits:
        raise AppError(400, "VALIDATION_ERROR", "No sections were provided.")
    patch = {}
    for name, text in edits.items():
        if name not in base["sections"]:
            raise AppError(400, "VALIDATION_ERROR", "Unknown section.")
        if text == base["sections"][name]:
            continue
        if name in base["protected"]:
            raise AppError(400, "PROTECTED_SECTION", f"The {name} section cannot be changed.")
        if len(text) > MAX_SECTION_CHARS:
            raise AppError(400, "VALIDATION_ERROR", f"The {name} section exceeds {MAX_SECTION_CHARS} characters.")
        if UNSAFE.search(text) or any(prot.lower() in text.lower() for prot in base["protected"]):
            raise AppError(400, "UNSAFE_CONTENT", f"The {name} section contains instructions that are not allowed.")
        patch[name] = {"from": base["sections"][name], "to": text}
    if not patch:
        raise AppError(400, "NO_CHANGES", "Nothing differs from the base prompt. Use Reset to return to it.")
    prompt = render(apply_patch(base, patch), base["protected"])
    if len(prompt) > MAX_PROMPT_CHARS or (context_window and estimate_tokens(prompt) > context_window // 2):
        raise AppError(400, "PROMPT_TOO_LONG", "The resulting prompt is too long for the selected model.")
    return patch


def apply_patch(base, patch):
    """Base sections with the patch applied, or None if the patch no longer applies cleanly."""
    sections = dict(base["sections"])
    for name, change in patch.items():
        if name in base["protected"] or sections.get(name) != change["from"]:
            return None
        sections[name] = change["to"]
    return sections


def effective(conn, user_id):
    base = current_base(conn)
    p = db.patches.c
    row = conn.execute(
        select(p.version, p.patch_content).where(p.user_id == user_id, p.is_active)
    ).one_or_none()
    sections, patch_version, conflict = base["sections"], None, False
    if row:
        patched = apply_patch(base, row.patch_content)
        if patched is None:
            conflict = True
        else:
            sections, patch_version = patched, row.version
    return {
        "base_version": base["version"],
        "patch_version": patch_version,
        "conflict": conflict,
        "conflict_patch_version": row.version if conflict else None,
        "sections": sections,
        "protected_sections": base["protected"],
        "prompt": render(sections, base["protected"] if patch_version else None),
    }


def user_view(eff):
    """What a non-admin may see: protected section text is never sent to them."""
    sections = {k: v for k, v in eff["sections"].items() if k not in eff["protected_sections"]}
    return {**eff, "sections": sections, "prompt": render(sections)}


def diff(old, new, old_label="current", new_label="proposed"):
    # trailing newline so the last changed line doesn't merge its - and + halves
    return "".join(difflib.unified_diff(
        (old + "\n").splitlines(keepends=True), (new + "\n").splitlines(keepends=True), old_label, new_label
    ))


def save_patch(conn, user_id, base_version, patch):
    """Append a new active version for this user (patches are append-only)."""
    db.lock(conn, f"patch:{user_id}")
    p = db.patches.c
    version = conn.execute(
        select(func.coalesce(func.max(p.version), 0) + 1).where(p.user_id == user_id)
    ).scalar_one()
    conn.execute(update(db.patches).where(p.user_id == user_id, p.is_active).values(is_active=False))
    conn.execute(db.patches.insert().values(
        user_id=user_id, version=version, base_version=base_version, patch_content=patch, is_active=True,
    ))
    return version


def reset(conn, user_id):
    db.lock(conn, f"patch:{user_id}")
    p = db.patches.c
    conn.execute(update(db.patches).where(p.user_id == user_id, p.is_active).values(is_active=False))
