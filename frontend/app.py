"""Streamlit UI. Talks only to the backend API; never stores keys or calls a
provider.
"""

import os

import httpx
import streamlit as st

API = os.environ.get("LLM_AGENT_API", "http://localhost:8000/api/v1")

st.set_page_config(page_title="Multi-Provider LLM Agent", layout="wide")

# Production: paste an OIDC access token from your IdP. Dev (AUTH_MODE=dev): any
# user id.
# ponytail: no OAuth redirect flow; add one (e.g. st.login) when this UI is
# user-facing
if os.environ.get("LLM_AGENT_AUTH", "dev") == "dev":
    user = st.sidebar.text_input(
        "User ID (dev)",
        value="user1",
        help="Switch users to see per-user isolation.",
    )
    auth_headers = {"X-User-Id": user}
else:
    user = st.sidebar.text_input("Access token", type="password")
    auth_headers = {"Authorization": f"Bearer {user}"}
if not user:
    st.stop()


def call(method, path, quiet=False, **kw):
    """Returns JSON, or None after showing the API's error message (or, with
    quiet, keeping it in last_error).
    """
    try:
        r = httpx.request(
            method, API + path, headers=auth_headers, timeout=150, **kw
        )
    except httpx.HTTPError:
        st.error("The backend is not reachable.")
        st.stop()  # nothing else on the page can work; one message is enough
    try:
        body = r.json()
    except ValueError:
        # e.g. an HTML error page from a proxy in front of the API
        body = {
            "message": "The service returned an unexpected response.",
            "code": f"HTTP_{r.status_code}",
        }
    if r.status_code >= 400:
        msg = (
            f"{body.get('message', 'Request failed.')}  \n"
            f"`{body.get('code')}` · ref {body.get('correlationId')}"
        )
        if quiet:
            st.session_state["last_error"] = msg
        else:
            st.error(msg)
        return None
    return body


tab_model, tab_prompt, tab_run = st.tabs(
    ["Provider & API Key", "System Prompt", "Run Agent"]
)

# ---------- Phase 1 ----------
with tab_model:
    plist = call("GET", "/providers") or []
    if plist:
        names = {p["id"]: p for p in plist}
        pref = call("GET", "/users/me/model-preference") or {}
        ids = list(names)
        pid = st.selectbox(
            "Provider",
            ids,
            index=(
                ids.index(pref["provider_id"])
                if pref.get("provider_id") in ids
                else 0
            ),
            format_func=lambda i: names[i]["name"],
            key=f"provider_{user}",
        )
        p = names[pid]
        status = p["key_status"]
        badge = {"Valid": "🟢", "Invalid": "🔴", "Not Set": "⚪"}[status]
        st.write(
            f"API key status: {badge} **{status}**"
            + (f" · ends in `{p['key_last4']}`" if p["key_last4"] else "")
        )

        with st.form(f"key_{pid}", clear_on_submit=True):
            key = st.text_input(
                "API key", type="password", placeholder="Paste your key"
            )
            label = "Replace key" if status != "Not Set" else "Save key"
            if st.form_submit_button(label) and key:
                with st.spinner("Validating key with provider..."):
                    if call(
                        "PUT",
                        f"/users/me/credentials/{pid}",
                        json={"api_key": key},
                    ):
                        st.rerun()
        if status != "Not Set" and st.button("Delete key"):
            call("DELETE", f"/users/me/credentials/{pid}")
            st.rerun()

        if status == "Valid":
            with st.spinner("Loading models..."):
                models = call("GET", f"/providers/{pid}/models")
            free_only = st.checkbox(
                "Free-tier models only", key=f"free_only_{user}"
            )
            if models and free_only:
                models = [m for m in models if m.get("free_tier")]
                if not models:
                    st.info(
                        f"No free-tier models are listed for "
                        f"{names[pid]['name']}. Calls are billed to your key."
                    )
            if models == [] and not free_only:
                st.info("This provider returned no models for your key.")
            elif models:
                ids = [m["id"] for m in models]
                free = [i for i, m in enumerate(models) if m.get("free_tier")]
                if (
                    pref.get("provider_id") == pid
                    and pref.get("model_id") in ids
                ):
                    idx = ids.index(pref["model_id"])
                else:
                    idx = (
                        free[0] if free else 0
                    # nothing saved yet: start on a free model when there is one
                    )
                model = st.selectbox("Model", ids, index=idx)
                if st.button("Use this model"):
                    if call(
                        "PUT",
                        "/users/me/model-preference",
                        json={"provider_id": pid, "model_id": model},
                    ):
                        st.success(
                            f"Agent will use {names[pid]['name']} / {model}."
                        )

        pref = call("GET", "/users/me/model-preference") or {}
        if pref.get("model_id"):
            st.caption(
                f"Active selection: {pref['provider_id']} / {pref['model_id']}"
            )

# ---------- Phase 2 ----------
with tab_prompt:
    eff = call("GET", "/users/me/system-prompt")
    if eff:
        v = f"base v{eff['base_version']}" + (
            f" + patch v{eff['patch_version']}"
            if eff["patch_version"]
            else " (no patch)"
        )
        st.write(f"Effective prompt: **{v}**")
        if eff["conflict"]:
            st.warning(
                f"The platform updated the base prompt and your patch "
                f"v{eff['conflict_patch_version']} "
                "no longer applies. The base prompt is in use until you apply "
                "your edits again or reset."
            )

        edits = {}
        for name, text in eff["sections"].items():
            protected = name in eff["protected_sections"]
            edits[name] = st.text_area(
                name + (" 🔒" if protected else ""),
                text,
                disabled=protected,
                key=f"sec_{user}_{name}_{eff['patch_version']}",
            )
        editable = {
            k: v for k, v in edits.items() if k not in eff["protected_sections"]
        }

        c1, c2 = st.columns(2)
        if c1.button("Preview changes"):
            prev = call(
                "POST",
                "/users/me/system-prompt/preview",
                json={"sections": editable},
            )
            if prev:
                left, right = st.columns(2)
                left.markdown("**Current**")
                left.code(prev["current"], language="markdown", wrap_lines=True)
                right.markdown("**Proposed**")
                right.code(
                    prev["resolved"], language="markdown", wrap_lines=True
                )
                st.code(prev["diff"] or "No differences.", language="diff")
        if c2.button("Apply", type="primary"):
            if call(
                "POST",
                "/users/me/system-prompt/patches",
                json={"sections": editable},
            ):
                st.rerun()

        st.divider()
        st.subheader("History")
        hist = call("GET", "/users/me/system-prompt/patches") or []
        # A confirmation covers one action: untick it after each reset/revert.
        confirm_key = f"confirm_{user}"
        if st.session_state.pop("clear_confirm", False):
            st.session_state[confirm_key] = False
        confirm = st.checkbox("I confirm revert / reset", key=confirm_key)
        if st.button("Reset to base prompt", disabled=not confirm):
            call("DELETE", "/users/me/system-prompt/patches")
            st.session_state["clear_confirm"] = True
            st.rerun()
        for h in hist:
            with st.expander(
                f"v{h['version']} · "
                f"{h['created_at'][:19].replace('T', ' ')} · "
                f"base v{h['base_version']}"
                + (" · active" if h["is_active"] else "")
            ):
                for name, text in h["sections"].items():
                    st.markdown(f"**{name}**")
                    st.text(text)
                if not h["is_active"] and st.button(
                    f"Revert to v{h['version']}",
                    key=f"rv{h['version']}",
                    disabled=not confirm,
                ):
                    url = f"/users/me/system-prompt/patches/{h['version']}"
                    if call("POST", f"{url}/revert"):
                        st.session_state["clear_confirm"] = True
                        st.rerun()

# ---------- Execution ----------
with tab_run:
    task = st.text_area("Task", height=150)
    if "run_error" in st.session_state:
        st.error(st.session_state.pop("run_error"))
    if st.button("Run", type="primary", disabled=not task.strip()):
        with st.spinner("Running..."):
            res = call(
                "POST", "/agent/execute", quiet=True, json={"task": task}
            )
        if res is None:
            # rerun so the other tabs (e.g. key status) reflect what this
            # failure changed
            st.session_state["run_error"] = st.session_state.pop("last_error")
            st.rerun()
        else:
            pv = (
                f"patch v{res['patch_version']}"
                if res["patch_version"]
                else "no patch"
            )
            st.caption(
                f"{res['provider_id']} / {res['model_id']} · prompt base "
                f"v{res['base_version']}, {pv}"
                f" · tokens in {res['tokens_in']} / out {res['tokens_out']}"
            )
            if res["prompt_conflict"]:
                st.warning(
                    "Your prompt patch is out of date; the base prompt was "
                    "used."
                )
            st.markdown(res["output"])
