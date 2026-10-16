"""Deploy-time commands. Run once per deploy, never from the app at runtime.

    python -m backend.manage migrate            # schema + provider reference data + base prompt
    python -m backend.manage publish-base FILE  # publish a new base prompt version
    python -m backend.manage gen-kek            # print a new key-encryption key
    python -m backend.manage rewrap             # re-wrap every data key with the current KEK (rotation)
"""
import json
import sys
from pathlib import Path

from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = Path(__file__).with_name("base_prompt.json")


def migrate():
    from alembic import command
    from alembic.config import Config

    from backend import db, providers

    if db.engine.dialect.name == "sqlite":
        Path(db.engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    command.upgrade(cfg, "head")
    with db.engine.begin() as conn:
        for pid, p in providers.PROVIDERS.items():
            db.upsert(conn, db.providers, {"id": pid, "name": p["name"], "base_url": p["base_url"]}, ["id"])
    publish_base(DEFAULT_BASE)


def publish_base(path):
    from backend import db, prompts

    data = json.loads(Path(path).read_text())
    sections, protected = data["sections"], data["protected_sections"]
    if not all(isinstance(v, str) and v.strip() for v in sections.values()) or not set(protected) <= set(sections):
        sys.exit("Invalid base prompt: every section needs text and protected_sections must name sections.")
    with db.engine.begin() as conn:
        print(f"Current base prompt version: {prompts.publish_base(conn, sections, protected)}")


def rewrap():
    from backend import crypto, db

    c = db.credentials.c
    kid = crypto.current_kek_id()
    with db.engine.begin() as conn:
        rows = conn.execute(select(c.id, c.wrapped_dek, c.kek_id).where(c.kek_id != kid).with_for_update()).all()
        for r in rows:
            new_kid, wrapped = crypto.wrap_dek(crypto.unwrap_dek(r.kek_id, r.wrapped_dek))
            conn.execute(update(db.credentials).where(c.id == r.id).values(wrapped_dek=wrapped, kek_id=new_kid))
    print(f"Re-wrapped {len(rows)} keys with KEK {kid}.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "gen-kek":
        from backend import crypto
        return print(crypto.new_kek())
    from backend import config
    config.validate()
    if cmd == "migrate":
        migrate()
    elif cmd == "publish-base" and len(argv) == 3:
        publish_base(argv[2])
    elif cmd == "rewrap":
        rewrap()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
