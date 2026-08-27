#!/usr/bin/env python3
"""
iSmart-LA bootstrap -- generate this deployment's environment brief.

Run once after install (install.sh calls it), or again any time you want to
redo the brief from scratch.

WHY THIS EXISTS
    SOUL.md (Claude's brief) and GEMINI.md (agy's brief) tell the agent what it
    is looking after, how to reach it, and what it must never touch. Those files
    are the one genuinely per-deployment part of this system -- everything else
    installs identically anywhere. Hand-authoring them for every new server is
    the friction this script removes.

WHAT IT DOES *NOT* DO
    It does not ask a model to invent your safety rules. The "never touch this"
    section is written verbatim from what YOU type, into a zone the agent can
    never edit. A model paraphrasing a hard boundary into something subtly
    weaker is not a failure mode worth risking to save you some typing.

    Discovery (the optional last step) only fills the LEARNED zone below the
    marker -- facts about tooling and topology the agent verified for itself.

NOT PROXMOX-SPECIFIC
    Nothing here assumes Proxmox, or infrastructure at all. Describe whatever
    you want the agent to look after in your own words and it becomes the brief.
    examples/proxmox/ is one filled-in example, not a required shape.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SOUL = BASE_DIR / "SOUL.md"
GEMINI = BASE_DIR / "GEMINI.md"
LEARNED_ZONE_MARKER = "<!-- LEARNED_ZONE -->"

# Tool-name differences between the two CLIs. Same brief, right vocabulary for
# each side, so neither is told to call something that doesn't exist for it.
DIALECT = {
    "soul": {
        "run": "the Bash tool",
        "write": "the Write tool",
        "name": "Claude Code",
    },
    "gemini": {
        "run": "`run_command`",
        "write": "`write_to_file`",
        "name": "the Antigravity CLI",
    },
}


def ask(prompt: str, *, multiline: bool = False, required: bool = True) -> str:
    """Ask until we get something usable. Multiline input ends with a blank line."""
    while True:
        print(f"\n{prompt}")
        if multiline:
            print("(finish with an empty line)")
            lines: list[str] = []
            while True:
                try:
                    line = input("  ")
                except EOFError:
                    break
                if not line.strip():
                    break
                lines.append(line.strip())
            answer = "\n".join(lines).strip()
        else:
            try:
                answer = input("  > ").strip()
            except EOFError:
                answer = ""
        if answer or not required:
            return answer
        print("  ! This one is needed -- please answer.")


def ask_list(prompt: str) -> list[str]:
    raw = ask(prompt, multiline=True, required=False)
    return [ln.lstrip("-•* \t") for ln in raw.split("\n") if ln.strip()]


def build_brief(answers: dict, dialect: str) -> str:
    d = DIALECT[dialect]
    role = answers["role"]
    access = answers["access"]
    boundaries = answers["boundaries"]
    destructive = answers["destructive"]
    notes = answers["notes"]

    out: list[str] = []
    out.append(
        f"You are an assistant looking after: {role}\n\n"
        "You are helpful, knowledgeable, and direct. You investigate, check status, and "
        "answer questions about the environment described below. You communicate clearly, "
        "admit uncertainty rather than guessing, and prefer being genuinely useful over "
        "being verbose. Be targeted and efficient -- every extra exploratory step costs "
        "real quota."
    )

    out.append("\n## How to reach this environment\n")
    out.append(access)
    out.append(
        f"\nUse {d['run']} to run commands. Do not go looking around the local filesystem "
        "for answers that live in the environment described above."
    )

    out.append("\n## HARD BOUNDARIES -- never do these without explicit human confirmation in the moment, even if asked generically\n")
    if boundaries:
        for b in boundaries:
            out.append(f"- {b}")
    else:
        out.append(
            "- (None recorded at install time. Add them here by hand as you discover "
            "what must not be touched -- this section is deliberately outside anything "
            "the agent can edit.)"
        )
    if destructive:
        out.append(
            f"- {destructive}"
        )
    out.append(
        "\nRead-only checks are fine without asking. Anything that changes state, "
        "deletes, restarts, or writes to the managed environment requires explicit "
        "human approval first -- ask, then wait for a clear yes."
    )

    if notes:
        out.append("\n## Other things to know\n")
        for n in notes:
            out.append(f"- {n}")

    out.append(f"""
## Generating and delivering reports

When asked for a report, summary, or document as a file, actually produce it -- do not
just describe the steps.

1. Gather the data yourself.
2. Compile it into an HTML file with {d['write']} -- clean semantic HTML, a simple
   embedded <style> block, no external assets.
3. Deliver it by putting a line by itself in your final reply reading exactly
   `MEDIA:` followed by the absolute file path.

If asked for images rather than a document, build a **slide deck**: several separate
1920x1080 HTML pages, each rendered to its own JPEG, paginated by content so nothing
overflows the slide. Never produce one enormous full-page screenshot, never slice a
long page into strips, and never output PNG -- a full-page PNG routinely lands over
Telegram's 50MB limit and gets discarded, where the same content as JPEG is 1-2MB.
Past ~3 files, zip them and deliver the archive.

## Batch independent tool calls together

When a task needs several INDEPENDENT actions, request them as parallel calls in the
SAME turn rather than one at a time. Every extra round trip re-sends the whole growing
conversation, which is the single biggest avoidable cost here. Go sequential only when
a later call genuinely depends on an earlier result.

## Recording what you learn about this environment

You start each new conversation with this brief and nothing else. Anything you work out
by probing is lost when the conversation ends -- unless you record it.

When you discover something durable and reusable about THIS environment, add a line by
itself in your final reply:

```
LEARN: <one specific, self-contained fact>
```

That line is stripped from what the user sees and appended to the Learned section
below, so every future conversation starts already knowing it.

**Record:** which tools are and aren't installed plus their exact working invocation;
corrected topology; where a service really keeps its config/logs; a command form that
works here versus one that silently fails; naming conventions; the shape of a report
someone asks for repeatedly.

**Do not record:** anything transient (current load, today's counts), anything specific
to a single request, guesses you have not verified, or any secret or credential. If
you would have to re-check it to trust it tomorrow, it is not a fact.

One fact per line, under ~400 characters, phrased to still make sense read cold months
later. Emit no LEARN: line at all when you learned nothing durable -- that is the normal
case for a routine question.

You cannot edit this brief, and writing to it with shell commands is out of bounds: the
boundaries above are deliberately outside your control. LEARN: is the supported path,
and it only ever appends below the marker.

{LEARNED_ZONE_MARKER}
## Learned about this environment

Facts the agent discovered and recorded itself. Safe to edit or delete by hand --
nothing above the marker line is ever touched automatically.

""")
    return "\n".join(out)


def run_discovery(answers: dict) -> None:
    """Optional: let the agent probe and record what it finds, once."""
    agy = os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
    if not Path(agy).exists():
        print("\n! agy not found -- skipping discovery. The agent will learn as you use it.")
        return

    prompt = (
        "This is your first run in a new environment. Your brief describes it, but "
        "nothing has been verified yet.\n\n"
        "Do a SHORT, focused discovery pass -- a handful of cheap commands, not an "
        "exhaustive audit:\n"
        "1. Confirm you can actually reach the environment described in your brief.\n"
        "2. Check which of the tools you would typically need are installed, and their "
        "exact invocation.\n"
        "3. Note anything that differs from what the brief assumes.\n\n"
        "Then finish with one LEARN: line per durable fact you verified. Keep the "
        "visible reply to a few sentences -- the LEARN: lines are the real output. "
        "Do NOT change anything; this is read-only."
    )
    print("\nRunning a short discovery pass (this costs a little quota)...")
    try:
        proc = subprocess.run(
            [agy, "-p", prompt, "--output-format", "text", "--print-timeout", "240s"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        print("! Discovery timed out. Skipping -- the agent will learn as you use it.")
        return

    out = (proc.stdout or "").strip()
    facts = [
        ln.split("LEARN:", 1)[1].strip()
        for ln in out.split("\n")
        if ln.strip().startswith("LEARN:")
    ]
    if not facts:
        print("! Discovery returned nothing durable. That's fine -- it learns as you use it.")
        return

    for path in (SOUL, GEMINI):
        text = path.read_text()
        head, _, _ = text.partition(LEARNED_ZONE_MARKER)
        body = "\n".join(f"- [bootstrap] {f}" for f in facts)
        path.write_text(
            f"{head}{LEARNED_ZONE_MARKER}\n## Learned about this environment\n\n"
            "Facts the agent discovered and recorded itself. Safe to edit or delete by\n"
            "hand -- nothing above the marker line is ever touched automatically.\n\n"
            f"{body}\n"
        )
    print(f"\nRecorded {len(facts)} verified fact(s):")
    for f in facts:
        print(f"  • {f}")


def main() -> None:
    print(__doc__)
    if SOUL.exists() or GEMINI.exists():
        print("! SOUL.md / GEMINI.md already exist.")
        if input("  Overwrite them? [y/N] ").strip().lower() != "y":
            print("Left alone. Nothing changed.")
            return

    print("\n" + "=" * 70)
    print("Describe this deployment in your own words. Plain sentences are fine --")
    print("this text goes straight into the agent's brief, it is not parsed.")
    print("=" * 70)

    answers = {
        "role": ask(
            "1/5  What should this agent look after?\n"
            "     e.g. 'a 7-node Proxmox cluster', 'our Kubernetes staging cluster',\n"
            "     'a fleet of 20 Ubuntu web servers', 'the company's GitLab and CI runners'"
        ),
        "access": ask(
            "2/5  How does it reach that environment, concretely?\n"
            "     Include hostnames/IPs and the exact command shape that works.\n"
            "     e.g. 'SSH with keys already in ~/.ssh/config: ssh 10.0.0.11 <command>.\n"
            "           Nodes: node-a 10.0.0.11, node-b 10.0.0.12'\n"
            "     TIP: prefer raw IPs over hostnames unless you have checked what DNS\n"
            "     actually returns -- wildcard records pointing at a proxy are common.",
            multiline=True,
        ),
        "boundaries": ask_list(
            "3/5  What must the agent NEVER touch without asking first?\n"
            "     One per line. Be specific -- these are copied in verbatim and the\n"
            "     agent can never edit them.\n"
            "     e.g. 'The VM named prod-db -- never access, modify or stop it'"
        ),
        "destructive": ask(
            "4/5  Anything else that always needs human approval? (blank to skip)",
            required=False,
        ),
        "notes": ask_list(
            "5/5  Any other quirks worth knowing? One per line, blank to skip.\n"
            "     e.g. 'the name pm2 means a host, NOT the node.js process manager'"
        ),
    }

    SOUL.write_text(build_brief(answers, "soul"))
    GEMINI.write_text(build_brief(answers, "gemini"))
    print(f"\n✓ Wrote {SOUL.name} ({SOUL.stat().st_size} bytes)")
    print(f"✓ Wrote {GEMINI.name} ({GEMINI.stat().st_size} bytes)")

    print("\nOptional: a short discovery pass, where the agent probes the environment")
    print("once and records what it verifies. Costs a little quota; skip it and the")
    print("agent simply learns the same things as you use it.")
    if input("\n  Run discovery now? [y/N] ").strip().lower() == "y":
        run_discovery(answers)

    print("\nDone. Review the briefs before relying on them -- especially the")
    print("HARD BOUNDARIES section. Adjust any time by editing the files directly;")
    print("only the part below the LEARNED_ZONE marker is ever written automatically.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled. Nothing written.")
        sys.exit(1)
