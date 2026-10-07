UCSB Confluence page: https://ucsb-atlas.atlassian.net/wiki/x/lAHGowQ

# Prerequisites

- SSH access to source redcap instance
- API access to destination redcap instance
- Admin on both redcap servers
- `redcap-move-files.py` and `redcap-copy-users.py` scripts
- `uv` ([site and docs](https://astral.sh/))
  - Without `uv`, install `python3-requests` and use `python3 <script> ...`
- `ssh`, `curl`, and `python3` on the machine that runs the commands
- `mysql` client on the source server. `redcap-move-files.py` uses it over SSH.

# Environment variables

Set these in the shell before you use any command in this note. Change the values for each migration.

```bash
export REDCAP_SSH=bob@$redcapDST.example.com     # SSH user and source server
export REDCAP_SRC_URL=https://redcapSRC.example.com/api/       # source API
export REDCAP_DST_URL=https://redcapDST.example.com/api/           # destination API
```

# Prepping the projects

## On Source Redcap Instance

1. Move project Status to Analysis/Cleanup

`Project home page` -> `Other Functionality` -> `Move the project to Analysis/Cleanup status`

Ensure that the top banner says:

> The data in this project is currently: Read-only/Locked

Otherwise select `Modify` to change to Read-only/Locked

2. Export XML

`Other Export Options` -> `Download metadata & data (XML)`

- Do not select a de-identification option.
- Do not select `Download metadata only (XML)`. That file has no records. If you use it, the file move fails with `The record '1' does not exist`.

> [!warning]
> The XML file contains personal data. Keep it only as long as needed and delete it with `shred -u` when the migration is complete.

3. Get the project purpose

`Project Setup` -> `Modify project title, purpose, etc.`

Or, use the API:

```bash
curl -s -d token="$REDCAP_SRC_TOKEN" -d content=project -d format=json "$REDCAP_SRC_URL" | python3 -m json.tool | grep -E '"purpose'
```

| `purpose` | New Project form option            |
| --------- | ---------------------------------- |
| 0         | Practice / Just for fun            |
| 1         | Other (text is in `purpose_other`) |
| 2         | Research (also fill in IRB and PI) |
| 3         | Quality Improvement                |
| 4         | Operational Support                |

If the purpose is not set, use `Operational Support`. Do not use `Practice`.

4. Get an API token

`Applications` -> `API`. The token must have export rights and file download rights.

Do not regenerate an existing token. Regeneration stops the old token.

## On Destination Redcap Instance

1. Create the project

`New Project` -> upload the XML from source step 2. Set the purpose from source step 3.

> [!warning] 504 error after the XML upload
> Cause: the proxy in front of REDCap stops waiting before REDCap finishes the import. REDCap can continue to create the project after the 504.
>
> 1.  Do not upload the XML again. A second upload can make a duplicate project.
> 2.  Wait 5 to 15 minutes. Then refresh `My Projects`.
> 3.  If the project shows, make sure that all instruments, all events, and all records are there.
> 4.  If the project is missing after 15 minutes, or is not complete, delete it. Then upload again.

2. Check the project

- Make sure the record count is the same as the source (`Record Status Dashboard`).
- Make sure all instruments (`Online Designer`) and all events (`Define My Events`) are there.
- The XML does not include users. Not even the admin who uploaded it is a user.

3. Add yourself as a user

`User Rights` -> add your username. Give yourself User Rights and API import rights. You cannot get an API token until you are a user.

4. Get an API token

`Applications` -> `API`. The token must have import rights, file upload rights, and User Rights.

5. Copy the other users

`redcap-copy-users.py` copies roles, user rights, and role assignments from the source. It does not change users that are already on the destination (for example, you). Both tokens must have User Rights.

```bash
./redcap-copy-users.py --src-url "$REDCAP_SRC_URL" --dst-url "$REDCAP_DST_URL" --dry-run
./redcap-copy-users.py --src-url "$REDCAP_SRC_URL" --dst-url "$REDCAP_DST_URL"
```

Make sure the usernames in the dry run are correct for the destination server. Then examine `User Rights` on the destination.

# Moving the files

Do these steps on the jumpbox.

1. Load the tokens. The tokens stay in the shell only. Do not write them to disk.

```bash
read -rs REDCAP_SRC_TOKEN && export REDCAP_SRC_TOKEN   # source
read -rs REDCAP_DST_TOKEN && export REDCAP_DST_TOKEN   # destination
```

2. Change the signature fields to File Upload

REDCap does not import a file into a `Signature` field. On the destination, use the Online Designer to change each signature field to `File Upload`.

The list of signature fields is different for each project. `--check-signatures` shows the list. For CCSP APA Tracking, the 11 fields are:

```
advisor_sig, student_sig, rf_eval_sig_eval_2_eval_1, rf_eval_sig_eval_2_eval_2,
rf_eval_sig_eval_3_eval_3, doc_chair_sig, com_mem_sig_1, com_mem_sig_2,
com_mem_sig_3, com_mem_sig_4, qual_diss_student_sig
```

Make sure the result shows `0 Signature`:

```bash
./redcap-move-files.py --src-url "$REDCAP_SRC_URL" --dst-url "$REDCAP_DST_URL" --check-signatures
```

3. Do a dry run

```bash
./redcap-move-files.py --ssh "$REDCAP_SSH" \
    --src-url "$REDCAP_SRC_URL" --dst-url "$REDCAP_DST_URL" --include-signatures --dry-run
```

4. Move the files

```bash
./redcap-move-files.py --ssh "$REDCAP_SSH" \
    --src-url "$REDCAP_SRC_URL" --dst-url "$REDCAP_DST_URL" --include-signatures
```

To leave the session while the run continues, push `Ctrl-b`, then `d`. To go back, use `tmux attach -t redcap`.

Make sure the last line of the log (`redcap-move-files-<timestamp>.log`) is only `moved N/N file(s)`, with both numbers the same.

If there are problems, the last line also shows `N missing on source` or `N import failures`. To find the failed files, use `grep 'IMPORT FAILED' <log>`.

If the source project is Read-only/Locked, one run is enough.

5. Change the signature fields back to `Signature`. Then make sure `--check-signatures` shows `0 File Upload`.

# Finishing

1. Move the destination project to production status.

   1. Go to `Project Setup`.
   2. On each setup step, select `I'm done!`.
   3. On the last step, select `Move project to production`.
   4. In the dialog, select `Keep ALL data saved so far. (N records)`. Make sure N is the same as the source record count.

   > [!warning]
   > Do not select the option that deletes all data. That option deletes all records and all moved files.

2. Delete the XML file: `shred -u <xml-file>`
3. Delete any empty or failed destination projects.
4. Delete other data files on the jumpbox (for example, record exports): `shred -u <file>`
5. Tell the customer the new project URL.

# Troubleshooting

## `The record '1' does not exist. It must exist to upload a file`

Cause: the destination project has no records.

Do a check:

```bash
curl -s -d token="$REDCAP_DST_TOKEN" -d content=project -d format=json "$REDCAP_DST_URL" | python3 -m json.tool | grep -E '"(project_id|project_title)"'
python3 -c 'import json,sys; d=json.load(sys.stdin); print(len({list(r.values())[0] for r in d}), "records")' < <(curl -s -d token="$REDCAP_DST_TOKEN" -d content=record -d format=json -d type=flat "$REDCAP_DST_URL")
```

If the record count is 0, create the project again from `Download metadata & data (XML)`.

> [!note]
> Do not use `cut -d,` on a CSV record export. Text fields can contain line breaks, so the output breaks into fragments. Use JSON.

## API record import fails with validation errors

Example errors:

- `The value is not a valid category for degree`
- `Email address is not properly formatted.`
- `assess_completion ... does not follow the expected format`

Cause: some old values do not obey the current field rules. Someone changed the field type after users entered data. REDCap does not convert old values. The API record import checks every value, so it stops the full import. The XML project import accepts these values.

Do not copy the data dictionary to fix this. The source has the same rules.

Fix: create the project from `Download metadata & data (XML)`. This keeps the data the same as the source.

## Data dictionary diff shows many differences

Most differences are format only. The source uses `1, A | 2, B` and the destination uses `1, A|2, B`. To remove this noise:

```bash
diff <(sed 's/ | /|/g' dd-src.csv) dd-dst.csv
```

The `sed` also changes `|` in labels. Ignore those lines.
