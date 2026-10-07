#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""Copy user roles, user rights, and role assignments from one REDCap project to another over the API.

Users already on the destination are left alone (that includes you - so your own API rights never change).
Roles are matched by label: a source role whose label is missing on the destination is created there.

Both tokens need the User Rights privilege.

    export REDCAP_SRC_TOKEN=...   # source project
    export REDCAP_DST_TOKEN=...   # destination project

    ./redcap-copy-users.py --src-url https://redcap.ets.ucsb.edu/api/ --dst-url https://redcap.ucsb.edu/api/ --dry-run
"""

import argparse
import json
import os
import sys

import requests

VERSION = "0.1.0"  # bump MAJOR.MINOR.PATCH on changes
SESSION = requests.Session()
TIMEOUT = 120
# exported for display only; not part of the user import format
PERSONAL_KEYS = ("email", "firstname", "lastname")


def parse_arguments():
    """Parse CLI arguments.

    Returns:
        argparse.Namespace.
    """
    p = argparse.ArgumentParser(description="Copy REDCap user roles and user rights between projects via the API.")
    p.add_argument("--src-url", help="Source API endpoint")
    p.add_argument("--dst-url", help="Destination API endpoint")
    p.add_argument("--dry-run", action="store_true", help="Show what would be copied; change nothing")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--self-test", action="store_true", help="Run offline logic checks and exit")
    return p.parse_args()


def api(url, token, data):
    """POST to a REDCap API endpoint and return the parsed JSON.

    Args:
        url: API endpoint.
        token: project API token.
        data: request fields.

    Returns:
        Parsed JSON response.
    """
    r = SESSION.post(url, data=dict(data, token=token, format="json", returnFormat="json"), timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"{data['content']} {data.get('action', 'export')} failed: {r.status_code} {r.text[:500]}")
    return r.json()


def export(url, token, content):
    """Export one API content type (user, userRole, userRoleMapping)."""
    return api(url, token, {"content": content})


def import_rows(url, token, content, rows):
    """Import a list of rows for one API content type."""
    return api(url, token, {"content": content, "action": "import", "data": json.dumps(rows)})


def missing_roles(src_roles, dst_roles):
    """Return source roles whose label is not on the destination, ready to import as new roles.

    Args:
        src_roles: source userRole export.
        dst_roles: destination userRole export.

    Returns:
        List of role rows with an empty unique_role_name (REDCap then creates a new role).
    """
    have = {r["role_label"] for r in dst_roles}
    return [dict(r, unique_role_name="") for r in src_roles if r["role_label"] not in have]


def role_name_map(src_roles, dst_roles):
    """Map source unique_role_name -> destination unique_role_name by role label."""
    by_label = {r["role_label"]: r["unique_role_name"] for r in dst_roles}
    return {r["unique_role_name"]: by_label[r["role_label"]] for r in src_roles if r["role_label"] in by_label}


def new_users(src_users, dst_users):
    """Return source users not yet on the destination, without the personal display fields."""
    have = {u["username"] for u in dst_users}
    return [{k: v for k, v in u.items() if k not in PERSONAL_KEYS} for u in src_users if u["username"] not in have]


def new_mappings(src_mappings, usernames, role_map):
    """Return role assignments for the given users, translated to destination role names."""
    return [{"username": m["username"], "unique_role_name": role_map[m["unique_role_name"]]}
            for m in src_mappings if m["username"] in usernames and m.get("unique_role_name") in role_map]


def self_test():
    """Offline checks for role matching, user filtering, and mapping translation."""
    src_roles = [{"unique_role_name": "U-A", "role_label": "Faculty"}, {"unique_role_name": "U-B", "role_label": "Staff"}]
    dst_roles = [{"unique_role_name": "U-X", "role_label": "Faculty"}]
    assert missing_roles(src_roles, dst_roles) == [{"unique_role_name": "", "role_label": "Staff"}]
    assert role_name_map(src_roles, dst_roles) == {"U-A": "U-X"}
    src_users = [{"username": "me", "email": "e"}, {"username": "bob", "email": "e", "design": 1}]
    assert new_users(src_users, [{"username": "me"}]) == [{"username": "bob", "design": 1}]
    maps = [{"username": "bob", "unique_role_name": "U-A"}, {"username": "me", "unique_role_name": "U-A"},
            {"username": "ann", "unique_role_name": ""}]
    assert new_mappings(maps, {"bob", "ann"}, {"U-A": "U-X"}) == [{"username": "bob", "unique_role_name": "U-X"}]
    print(f"redcap-copy-users v{VERSION} self-test OK")


def main():
    """Copy missing roles, then missing users, then their role assignments."""
    args = parse_arguments()
    if args.self_test:
        self_test()
        return
    src_token = os.environ.get("REDCAP_SRC_TOKEN")
    dst_token = os.environ.get("REDCAP_DST_TOKEN")
    if not (args.src_url and args.dst_url and src_token and dst_token):
        print("--src-url, --dst-url, REDCAP_SRC_TOKEN and REDCAP_DST_TOKEN are required", file=sys.stderr)
        sys.exit(1)

    src_roles = export(args.src_url, src_token, "userRole")
    dst_roles = export(args.dst_url, dst_token, "userRole")
    roles = missing_roles(src_roles, dst_roles)
    print(f"roles: {len(src_roles)} on source, {len(roles)} to create: {[r['role_label'] for r in roles]}")
    if roles and not args.dry_run:
        import_rows(args.dst_url, dst_token, "userRole", roles)
        dst_roles = export(args.dst_url, dst_token, "userRole")

    src_users = export(args.src_url, src_token, "user")
    users = new_users(src_users, export(args.dst_url, dst_token, "user"))
    print(f"users: {len(src_users)} on source, {len(users)} to add:")
    for u in src_users:
        if any(u["username"] == n["username"] for n in users):
            print(f"  {u['username']:<24} {u.get('firstname', '')} {u.get('lastname', '')} <{u.get('email', '')}>")

    usernames = {u["username"] for u in users}
    mappings = new_mappings(export(args.src_url, src_token, "userRoleMapping"), usernames, role_name_map(src_roles, dst_roles))
    print(f"role assignments to copy: {len(mappings)}")
    if args.dry_run:
        print("dry run - nothing changed")
        return
    if users:
        print(f"imported users: {import_rows(args.dst_url, dst_token, 'user', users)}")
    if mappings:
        print(f"imported role assignments: {import_rows(args.dst_url, dst_token, 'userRoleMapping', mappings)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, requests.RequestException) as e:
        print(e, file=sys.stderr)
        sys.exit(1)
