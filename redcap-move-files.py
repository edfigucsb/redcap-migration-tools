#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests"]
# ///
"""Copy uploaded files/signatures for one REDCap project to another, enumerating via the source DB over SSH.

Reads the exact list of attachments from the source database (READ-ONLY, over SSH so no tunnel is needed
and the DB password never leaves the box), then transfers each file: exportFile from the source API ->
importFile to the destination API. Record names AND events must already match on both sides (the metadata
& data XML import guarantees that, since the ODM carries events/arms).

The destination is API-only on purpose. Only the SOURCE box is SSHed.

    export REDCAP_SRC_TOKEN=...   # source project, API export incl. file rights
    export REDCAP_DST_TOKEN=...   # destination project, API import incl. file rights

    ./redcap-move-files.py --ssh eduardo@redcap1.ets.ucsb.edu \
        --src-url https://redcap.ets.ucsb.edu/api/ --dst-url https://dev.redcap.ucsb.edu/api/ --dry-run

Handles classic and longitudinal/multi-arm projects (each file carries its event). It STOPS if it detects
repeating instances, which need repeat_instance handling this script does not do - rather than risk
filing a document at the wrong coordinate.
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime

import requests

VERSION = "0.2.1"  # bump MAJOR.MINOR.PATCH on changes
SESSION = requests.Session()
TABLE_RE = re.compile(r"redcap_data\d*")
TIMEOUT = 120
SSH_TIMEOUT = 300
LOG_FILE = None  # run-log file handle; opened in main()

# Runs on the source box via `ssh <dest> bash -s -- <pid> <data_table> <database.php>`.
REMOTE = r'''
set -uo pipefail
PID="$1"; DATA_TABLE="$2"; DBPHP="$3"
command -v mysql >/dev/null 2>&1 || { echo "STATUS: nomysql"; exit 0; }
# only reach for sudo if we can't already read the file directly - sudo may be installed
# without a passwordless rule for us, and `sudo -n` then fails silently (confirmed on redcap1).
SUDO=""; [ -r "$DBPHP" ] || { [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo -n"; }
# ^[[:space:]]* anchors to a real assignment: database.php ships a commented example
# ($hostname = 'example.com:3307') ABOVE the real line, and an unanchored match grabbed that first.
val() { $SUDO grep -oP "^[[:space:]]*\\\$$1[[:space:]]*=[[:space:]]*'\K[^']*" "$DBPHP" 2>/dev/null | head -1; }
DBHOST="$(val host)"; [ -z "$DBHOST" ] && DBHOST="$(val hostname)"
DBNAME="$(val db)"; DBUSER="$(val username)"; DBPASS="$(val password)"
DBPORT="$(val db_port)"; [ -z "$DBPORT" ] && DBPORT="$(val port)"
case "$DBHOST" in *:*) [ -z "$DBPORT" ] && DBPORT="${DBHOST##*:}"; DBHOST="${DBHOST%:*}";; esac
if [ -z "$DBUSER" ] || [ -z "$DBNAME" ]; then echo "STATUS: nocreds"; exit 0; fi
MYCNF="$(mktemp)"; chmod 600 "$MYCNF"; trap 'rm -f "$MYCNF"' EXIT
{ printf '[client]\nconnect_timeout=10\nhost=%s\nuser=%s\npassword=%s\n' "${DBHOST:-localhost}" "$DBUSER" "$DBPASS"
  [ -n "$DBPORT" ] && printf 'port=%s\nprotocol=TCP\n' "$DBPORT"; } > "$MYCNF"
# </dev/null: this runs under `ssh bash -s` (script on stdin); without it mysql inherits and blocks on the
# still-open SSH stdin channel -> hang. --defaults-file reads ONLY our config (ignores /etc/my.cnf, ~/.my.cnf).
q() { mysql --defaults-file="$MYCNF" -N -B "$DBNAME" -e "$1" </dev/null 2>/dev/null; }
probe() { mysql --defaults-file="$MYCNF" -N -B "$DBNAME" -e "SELECT 1" </dev/null 2>&1; }
# if the parsed connection fails on a local host, retry over TCP - mysqld may listen on 127.0.0.1 while
# the CLI socket path misfires (confirmed on redcap1).
OUT="$(probe)" || {
  case "${DBHOST:-localhost}" in
    localhost|127.0.0.1|"")
      { printf '[client]\nconnect_timeout=10\nhost=127.0.0.1\nport=%s\nprotocol=TCP\nuser=%s\npassword=%s\n' "${DBPORT:-3306}" "$DBUSER" "$DBPASS"; } > "$MYCNF"
      OUT="$(probe)" || { echo "STATUS: dberror"; printf '%s\n' "$OUT" | head -3; exit 0; } ;;
    *) echo "STATUS: dberror"; printf '%s\n' "$OUT" | head -3; exit 0 ;;
  esac
}
# repeating instances (instance IS NOT NULL) need repeat_instance handling we don't do - refuse rather than mis-file.
[ "$(q "SELECT COUNT(*) FROM $DATA_TABLE WHERE project_id=$PID AND instance IS NOT NULL")" != "0" ] && { echo "STATUS: repeating"; exit 0; }
echo "STATUS: ok"
q "SELECT d.record, d.field_name, d.event_id, md.element_validation_type, m.doc_name FROM $DATA_TABLE d JOIN redcap_metadata md ON md.project_id=d.project_id AND md.field_name=d.field_name JOIN redcap_edocs_metadata m ON m.doc_id=d.value AND m.project_id=d.project_id WHERE d.project_id=$PID AND md.element_type='file' AND m.delete_date IS NULL ORDER BY d.record, d.field_name"
'''

STATUS_MSG = {
    "nomysql": "no mysql client found on the source box",
    "nocreds": "couldn't read creds from database.php on the box (SSH user needs read access, or passwordless sudo)",
    "dberror": "the box couldn't connect to the source DB using database.php creds",
    "repeating": "project uses repeating instances - not handled. Ask me to add repeat_instance support.",
}


def parse_arguments():
    """Parse CLI arguments.

    Returns:
        argparse.Namespace.
    """
    p = argparse.ArgumentParser(description="Move REDCap file uploads across servers via an SSH source-DB manifest.")
    p.add_argument("--ssh", help="SSH destination for the source box, e.g. user@redcap1.ets.ucsb.edu")
    p.add_argument("--src-url", help="Source API endpoint")
    p.add_argument("--dst-url", help="Destination API endpoint")
    p.add_argument("--project-id", type=int, help="Source project_id (auto-detected from the API if omitted)")
    p.add_argument("--database-php", default="/data/www/redcap/database.php", help="Path to database.php on the box")
    p.add_argument("--data-table", default="redcap_data", help="Project data table (rarely redcap_dataN)")
    p.add_argument("--dry-run", action="store_true", help="List what would move; transfer nothing")
    p.add_argument("--include-signatures", action="store_true",
                   help="Also import signature fields - ONLY after toggling them to File Upload on the destination")
    p.add_argument("--check-signatures", action="store_true",
                   help="Report whether each signature field is currently Signature or File Upload on the destination, then exit")
    p.add_argument("--log-file", help="Write the run log here (default: redcap-move-files-<timestamp>.log in the cwd)")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    p.add_argument("--self-test", action="store_true", help="Run offline logic checks and exit")
    return p.parse_args()


def api(url, token, data, files=None):
    """POST to a REDCap API endpoint."""
    payload = dict(data, token=token, returnFormat="json")
    return SESSION.post(url, data=payload, files=files, timeout=TIMEOUT)


def detect_project_id(url, token):
    """Return the project_id the source token belongs to."""
    r = api(url, token, {"content": "project", "format": "json"})
    r.raise_for_status()
    return int(r.json()["project_id"])


def events_to_map(events):
    """Build {event_id(str): unique_event_name} from an exportEvents list."""
    return {str(e["event_id"]): e["unique_event_name"]
            for e in events if "event_id" in e and "unique_event_name" in e}


def event_map(url, token):
    """Return {event_id: unique_event_name}; empty {} for classic (non-longitudinal) projects."""
    try:
        events = api(url, token, {"content": "event", "format": "json"}).json()
    except ValueError:
        return {}
    return events_to_map(events) if isinstance(events, list) else {}


def field_forms(url, token):
    """Return ({field: (order_index, form_name, field_type, validation)}, {form_name: label}).

    order_index is the field's position in project field order (instruments in order, fields within).
    Empty dicts if the calls fail - callers fall back to bare field names.
    """
    try:
        meta = api(url, token, {"content": "metadata", "format": "json"}).json()
        insts = api(url, token, {"content": "instrument", "format": "json"}).json()
    except Exception:
        return {}, {}
    fields = {m["field_name"]: (i, m.get("form_name", ""), m.get("field_type", ""),
                                m.get("text_validation_type_or_show_slider_number", ""))
              for i, m in enumerate(meta)} if isinstance(meta, list) else {}
    labels = {r["instrument_name"]: r["instrument_label"] for r in insts} if isinstance(insts, list) else {}
    return fields, labels


def order_toggle_fields(sig_fields, fields, labels):
    """Return [(instrument_label, field_name), ...] for signature fields, in project field order."""
    out = []
    for f in sorted(sig_fields):
        info = fields.get(f, (10**9, "", "", ""))
        out.append((info[0], labels.get(info[1], info[1]), f))
    out.sort()
    return [(lbl, f) for _, lbl, f in out]


def dest_state(dinfo):
    """Map a destination field's (idx, form, type, validation) tuple (or None) to a display state."""
    if dinfo is None:
        return "MISSING on dest"
    if dinfo[3] == "signature":
        return "Signature"
    if dinfo[2] == "file":
        return "File Upload"
    return dinfo[2] or "?"


def check_signature_fields(src_url, src_token, dst_url, dst_token):
    """Report each source signature field's current type on the destination (API-only; no SSH/DB)."""
    src, labels = field_forms(src_url, src_token)
    dst, _ = field_forms(dst_url, dst_token)
    rows = []
    for f, info in src.items():
        if info[2] == "file" and info[3] == "signature":   # a signature field on the source
            rows.append((info[0], labels.get(info[1], info[1]), f, dest_state(dst.get(f))))
    rows.sort()
    if not rows:
        log("no signature fields found on the source")
        return
    wl = max(len(lbl) for _, lbl, _, _ in rows)
    wf = max(len(f) for _, _, f, _ in rows)
    log(f"destination field type for the {len(rows)} source signature field(s):")
    for _, lbl, f, state in rows:
        log(f"  {lbl:<{wl}}  {f:<{wf}}  {state}")
    fu = sum(1 for r in rows if r[3] == "File Upload")
    sg = sum(1 for r in rows if r[3] == "Signature")
    log(f"{fu} File Upload (ready for --include-signatures), {sg} Signature (toggle needed), {len(rows) - fu - sg} other")


def parse_manifest(text):
    """Parse remote stdout into (status, [{record, field, event_id, doc_name}, ...], [diagnostics])."""
    status, rows, extra = "", [], []
    for ln in text.splitlines():
        if ln.startswith("STATUS:"):
            status = ln.split(":", 1)[1].strip()
        elif status == "ok" and ln.strip():
            parts = ln.split("\t")
            if len(parts) >= 4:
                rows.append({"record": parts[0], "field": parts[1], "event_id": parts[2],
                             "signature": parts[3] == "signature",
                             "doc_name": parts[4] if len(parts) > 4 else ""})
        elif ln.strip():
            extra.append(ln)
    return status, rows, extra


def remote_manifest(ssh_dest, pid, data_table, database_php):
    """SSH to the source box, run the manifest query, return (status, rows, diagnostics).

    stderr is left attached to the terminal so the ssh passphrase prompt and any ssh/sudo errors
    show live, rather than being captured (which looks like a hang).
    """
    proc = subprocess.run(
        ["ssh", ssh_dest, "bash", "-s", "--", str(pid), data_table, database_php],
        input=REMOTE.encode(), stdout=subprocess.PIPE, timeout=SSH_TIMEOUT,
    )
    if proc.returncode != 0 and b"STATUS:" not in proc.stdout:
        sys.exit("ssh/remote failed - see the ssh error above")
    return parse_manifest(proc.stdout.decode(errors="replace"))


def export_file(url, token, record, field, event):
    """Fetch one file from the source. Returns bytes, or None if the API returned an error/no file."""
    data = {"content": "file", "action": "export", "record": record, "field": field}
    if event:
        data["event"] = event
    r = api(url, token, data)
    # a real file comes back 200 with the file's own MIME; errors come back as application/json.
    if r.status_code != 200 or "application/json" in r.headers.get("Content-Type", ""):
        return None
    return r.content


def import_file(url, token, record, field, event, filename, blob):
    """Upload one file to the destination project at the same record/field/event."""
    data = {"content": "file", "action": "import", "record": record, "field": field}
    if event:
        data["event"] = event
    r = api(url, token, data, files={"file": (filename, blob)})
    if r.status_code != 200:
        raise RuntimeError(f"import failed {record}/{field}@{event or '-'}: {r.status_code} {r.text[:200]}")


def log(msg, err=False):
    """Print a timestamped line to stdout (or stderr) and, if open, the run-log file."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, file=sys.stderr if err else sys.stdout)
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def self_test():
    """Offline checks for the fragile bits: table guard, manifest parsing, event mapping."""
    assert TABLE_RE.fullmatch("redcap_data7") and not TABLE_RE.fullmatch("redcap_data; DROP")
    st, rows, extra = parse_manifest("STATUS: ok\n14\tadvisor_sig\t202\tsignature\tsig.png\n14\tstudent_cv\t202\tNULL\tcv.pdf\n")
    assert st == "ok" and len(rows) == 2
    assert rows[0] == {"record": "14", "field": "advisor_sig", "event_id": "202", "signature": True, "doc_name": "sig.png"}
    assert rows[1]["signature"] is False and rows[1]["doc_name"] == "cv.pdf"
    assert parse_manifest("STATUS: repeating\n") == ("repeating", [], [])
    assert parse_manifest("STATUS: dberror\nERROR 2003 nope\n") == ("dberror", [], ["ERROR 2003 nope"])
    assert events_to_map([{"event_id": 202, "unique_event_name": "year_1_student_dat_arm_1"}]) == {"202": "year_1_student_dat_arm_1"}
    ff = {"a_sig": (5, "fb", "file", "signature"), "b_sig": (2, "fa", "file", "signature"),
          "c_sig": (9, "fb", "file", "signature")}
    assert order_toggle_fields({"a_sig", "b_sig", "c_sig"}, ff, {"fa": "Form A", "fb": "Form B"}) \
        == [("Form A", "b_sig"), ("Form B", "a_sig"), ("Form B", "c_sig")]
    assert dest_state(None) == "MISSING on dest"
    assert dest_state((0, "f", "file", "signature")) == "Signature"
    assert dest_state((0, "f", "file", "")) == "File Upload"
    print(f"redcap-move-files v{VERSION} self-test OK")


def main():
    """Read the attachment manifest over SSH, resolve events, and re-attach each file on the destination."""
    args = parse_arguments()
    if args.self_test:
        self_test()
        return
    if not (args.src_url and args.dst_url):
        print("--src-url and --dst-url are required", file=sys.stderr)
        sys.exit(1)
    if not args.check_signatures and not args.ssh:
        print("--ssh is required (except with --check-signatures)", file=sys.stderr)
        sys.exit(1)
    if not TABLE_RE.fullmatch(args.data_table):
        print(f"refusing suspicious --data-table {args.data_table!r}", file=sys.stderr)
        sys.exit(1)
    src_token = os.environ.get("REDCAP_SRC_TOKEN")
    dst_token = os.environ.get("REDCAP_DST_TOKEN")
    if not (src_token and dst_token):
        print("set REDCAP_SRC_TOKEN and REDCAP_DST_TOKEN in the environment", file=sys.stderr)
        sys.exit(1)

    global LOG_FILE
    logpath = args.log_file or f"redcap-move-files-{datetime.now():%Y%m%d-%H%M%S}.log"
    LOG_FILE = open(logpath, "a")
    mode = " (check-signatures)" if args.check_signatures else (" (dry-run)" if args.dry_run else "")
    log(f"redcap-move-files v{VERSION}{mode} - logging to {logpath}")

    if args.check_signatures:
        check_signature_fields(args.src_url, src_token, args.dst_url, dst_token)
        return

    pid = args.project_id or detect_project_id(args.src_url, src_token)
    evmap = event_map(args.src_url, src_token)
    log(f"source project_id = {pid}; {len(evmap)} event(s); SSH to {args.ssh} for the manifest (may prompt for key passphrase)...")
    status, rows, extra = remote_manifest(args.ssh, pid, args.data_table, args.database_php)
    if status != "ok":
        detail = ("\n  " + "\n  ".join(extra)) if extra else ""
        sys.exit("STOP: " + STATUS_MSG.get(status, f"unexpected remote status {status!r}") + detail)

    total = len(rows)
    log(f"{total} live file attachment(s) in the source DB for project {pid}")
    moved = missing = failed = sigs = 0
    sig_fields = set()
    for i, row in enumerate(rows, 1):
        record, field = str(row["record"]), row["field"]
        event = evmap.get(row["event_id"], "")
        where = f"{record}/{field}@{event or '-'}"
        if row.get("signature") and not args.include_signatures:
            log(f"[{i}/{total}] SIGNATURE skipped (toggle field to File Upload + --include-signatures): {where}", err=True)
            sigs += 1
            sig_fields.add(field)
            continue
        blob = export_file(args.src_url, src_token, record, field, event)
        if blob is None:
            log(f"[{i}/{total}] MISSING on source API: {where} (db doc {row['doc_name']!r}) - skipped", err=True)
            missing += 1
            continue
        filename = row["doc_name"] or "file.bin"
        if args.dry_run:
            log(f"[{i}/{total}] would move {where} <- {filename} ({len(blob)} bytes)")
            moved += 1
            continue
        try:
            import_file(args.dst_url, dst_token, record, field, event, filename, blob)
        except Exception as e:  # keep going on a single bad import; re-running overwrites, so retry is safe
            log(f"[{i}/{total}] IMPORT FAILED {where} <- {filename}: {e}", err=True)
            failed += 1
            continue
        moved += 1
        log(f"[{i}/{total}] moved {where} <- {filename} ({len(blob)} bytes)")
    summary = [f"{'would move' if args.dry_run else 'moved'} {moved}/{total} file(s)"]
    if sigs:
        summary.append(f"{sigs} signatures skipped")
    if missing:
        summary.append(f"{missing} missing on source")
    if failed:
        summary.append(f"{failed} import failures (grep 'IMPORT FAILED' to retry)")
    log("; ".join(summary))
    if sig_fields:
        fields, labels = field_forms(args.src_url, src_token)
        toggle = order_toggle_fields(sig_fields, fields, labels)
        width = max((len(lbl) for lbl, _ in toggle), default=0)
        log("signature fields to toggle to File Upload on the destination, by instrument (then re-run with --include-signatures):", err=True)
        for lbl, f in toggle:
            log(f"  {lbl:<{width}}  {f}", err=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
