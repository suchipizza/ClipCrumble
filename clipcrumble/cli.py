"""ClipCrumble CLI. Two human gates: after `parse` (OK the to-do) and after `ideate` (approve concept ids)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

import typer

from . import briefparse, ideate as ideatemod, ingest as ingestmod, render as rendermod, report as reportmod
from . import schedule as schedmod, todo
from .util import REPO, Campaign, load_yaml

app = typer.Typer(add_completion=False, help="Brief + footage -> rendered clips, Metricool schedule, human to-do list. Never posts.")


@app.command()
def new(slug: str, brief: Optional[Path] = typer.Option(None, help="brief .md to copy in")):
    """Scaffold campaigns/<slug>/ with run.yaml from the template."""
    c = Campaign(slug).ensure()
    if not (c.root / "run.yaml").exists():
        txt = (REPO / "templates" / "run.yaml").read_text().replace("<slug>", slug)
        (c.root / "run.yaml").write_text(txt)
    if brief:
        shutil.copy(brief, c.root / "brief.md")
    typer.echo(f"created {c.root}. Next: put the brief in brief.md, edit run.yaml, then `clipcrumble parse {slug}`")


@app.command()
def parse(slug: str, force: bool = typer.Option(False, help="re-extract (overwrites hand edits)")):
    """brief.md -> brief.yaml (+ quote check) and todo.md pre-flight. STOP: read todo.md before going on."""
    c = Campaign(slug).ensure()
    briefparse.run(c, force=force)
    todo.write(c)
    typer.echo((c.out / "todo.md").read_text())
    typer.echo("STOP — check out/todo.md and brief.yaml (Unknowns). Continue with `clipcrumble ingest` when OK.")


@app.command()
def ingest(slug: str, from_: List[Path] = typer.Option(None, "--from", help="local footage folders/zips to import"),
           skip_transcribe: bool = False):
    """Fetch (Drive/YouTube only) or import footage; inventory, scenes, frames, dedupe, transcript."""
    c = Campaign(slug)
    try:
        ingestmod.run(c, from_ or None, skip_transcribe)
    except ingestmod.NeedsHuman as e:
        todo.write(c, [str(e)]); raise typer.Exit(f"needs you: {e}")
    todo.write(c)


@app.command()
def ideate(slug: str, check: bool = typer.Option(False, help="validate ideas.json and run the gates")):
    """Render the ideation prompt; with --check, validate ideas.json + gates and write out/ideas.md. STOP for approval."""
    c = Campaign(slug)
    if not check:
        p = ideatemod.render_prompt(c)
        typer.echo(f"prompt: {p}\nThe strategist (Claude) reads it plus media/working/sheets/*.jpg and writes "
                   f"media/working/ideas.json; then run `clipcrumble ideate {slug} --check`.")
        return
    rep = ideatemod.check(c)
    typer.echo(f"kept {rep['kept']}\ndropped {list(rep['dropped'])}\nsee {c.out / 'ideas.md'}\n"
               f"STOP — approve with `clipcrumble render {slug} --approve c01,c03`")


@app.command()
def render(slug: str, approve: str = typer.Option(..., help="comma-separated concept ids"), force: bool = False,
           workers: int = 4):
    """Render approved concepts (all platform siblings), QA, manifest, contact sheets."""
    c = Campaign(slug)
    res = rendermod.run(c, [x.strip() for x in approve.split(",") if x.strip()], workers, force)
    bad = [r["tag"] for r in res if not r["pass"]]
    typer.echo(f"{len(res) - len(bad)}/{len(res)} PASS" + (f"; FAIL: {bad}" if bad else "")
               + f"\nLook at {c.working / 'qa_sheets'} before scheduling.")


@app.command()
def schedule(slug: str, dry_run: bool = typer.Option(False, help="no upload, no Metricool")):
    """Slots -> Drive upload -> metricool.csv -> Metricool API (if configured) -> calendar.ics -> tracker."""
    s = schedmod.run(Campaign(slug), dry_run)
    typer.echo(f"route={s['route']} posts={len(s['posts'])} failed={s['failed']}")
    for n in s["notes"]:
        typer.echo(f"note: {n}")


@app.command()
def report(slug: str):
    """review_bundle.md + self-check PASS/FAIL."""
    ok = reportmod.run(Campaign(slug))
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(slug: str):
    """Where the campaign is in the pipeline."""
    c = Campaign(slug)
    steps = [("brief.yaml", c.root / "brief.yaml"), ("inventory", c.working / "inventory.json"),
             ("transcript", c.working / "transcript.json"), ("frames", c.working / "frames.json"),
             ("vision review", c.working / "frames_review.json"), ("ideas.json", c.working / "ideas.json"),
             ("gates", c.working / "ideas.gates.json"), ("schedule", c.out / "schedule.json"),
             ("review bundle", c.out / "review_bundle.md")]
    for name, p in steps:
        typer.echo(f"{'✓' if p.exists() else '·'} {name}")
    typer.echo(f"rendered: {len(list(c.ready.glob('*.mp4')))} mp4")


if __name__ == "__main__":
    app()
