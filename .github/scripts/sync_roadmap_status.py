"""
Regenerates docs/assets/roadmap-status.json from the real state of every
[Week N] section issue in this repo. Run by .github/workflows/sync-roadmap-status.yml
on issue webhook events + a daily schedule, using the workflow's own
GITHUB_TOKEN (authenticated, 5,000 req/hour) via `gh` -- never called from
the browser, so site visitors never touch a rate limit.

Status mapping:
  CLOSED + state_reason=NOT_PLANNED -> "wontfix"   (couldn't fix / archived)
  CLOSED (otherwise)                -> "done"
  OPEN + "blocked" label            -> "blocked"
  OPEN + "in-progress" label        -> "in_progress"
  OPEN (neither)                    -> "planned"
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "THEROCKSSS/smart-bulb-dashboard"
TITLE_RE = re.compile(r"^\[Week (\d)\]\s+(.+?)\s+\(W\d-(\d+)\s*[–-]\s*(\d+)\)$")


def gh_issues():
    # explicit utf-8 -- gh's JSON output is UTF-8, but subprocess's default
    # text-mode decoding follows the OS locale (cp1252 on Windows), which
    # mangles the en-dash in every issue title into garbage bytes. Found by
    # actually running this and seeing every title fail to parse.
    result = subprocess.run(
        [
            "gh", "issue", "list", "--repo", REPO, "--state", "all",
            "--label", "roadmap", "--limit", "200",
            "--json", "number,title,body,state,stateReason,labels,url,updatedAt,milestone",
        ],
        capture_output=True, check=True,
    )
    return json.loads(result.stdout.decode("utf-8"))


def classify(issue):
    label_names = {l["name"] for l in issue["labels"]}
    if issue["state"] == "CLOSED":
        return "wontfix" if (issue.get("stateReason") or "").upper() == "NOT_PLANNED" else "done"
    if "blocked" in label_names:
        return "blocked"
    if "in-progress" in label_names:
        return "in_progress"
    return "planned"


def first_paragraph(body):
    # Issue bodies are "<description>\n\nFull itemized list in `roadmap/...`, ..." --
    # the first paragraph is the real human-written one-liner, everything after
    # is boilerplate scaffolding shared by every issue and not worth surfacing.
    if not body:
        return ""
    return body.strip().split("\n\n")[0].strip()


def parse_issue(issue):
    m = TITLE_RE.match(issue["title"])
    if not m:
        print(f"  skip (title doesn't match pattern): {issue['title']}", file=sys.stderr)
        return None
    week = int(m.group(1))
    section = m.group(2)
    start_num, end_num = int(m.group(3)), int(m.group(4))
    milestone = issue.get("milestone")
    return {
        "issueNumber": issue["number"],
        "week": week,
        "section": section,
        "description": first_paragraph(issue.get("body", "")),
        "startNum": start_num,
        "endNum": end_num,
        "itemCount": end_num - start_num + 1,
        "status": classify(issue),
        "url": issue["url"],
        "updatedAt": issue["updatedAt"],
        "milestone": milestone["title"] if milestone else None,
    }


def main():
    issues = gh_issues()
    sections = [s for s in (parse_issue(i) for i in issues) if s]
    sections.sort(key=lambda s: (s["week"], s["startNum"]))

    counts = {}
    for s in sections:
        counts[s["status"]] = counts.get(s["status"], 0) + 1

    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalSections": len(sections),
        "statusCounts": counts,
        "sections": sections,
    }

    repo_root = Path(__file__).resolve().parents[2]
    out_path = repo_root / "docs" / "assets" / "roadmap-status.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(sections)} sections to {out_path}")
    print(f"Status counts: {counts}")


if __name__ == "__main__":
    main()
