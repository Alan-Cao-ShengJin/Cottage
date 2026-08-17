"""Shared provider-neutral browser chrome for Cottage account and OAuth pages."""

from __future__ import annotations

import html

CSS = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --navy: #0b294e;
  --navy-dark: #071c36;
  --blue: #3e72ad;
  --cream: #f6f1e7;
  --paper: #fffdf8;
  --border: #d8d2c7;
  --muted: #6f7a88;
  --danger: #a33a3a;
}
* { box-sizing: border-box; }
body {
  min-height: 100vh;
  min-height: 100dvh;
  margin: 0;
  padding: clamp(1rem, 4vw, 3rem) 1rem;
  background:
    radial-gradient(circle at 50% 0, rgba(62,114,173,.10), transparent 34rem),
    linear-gradient(rgba(16,43,79,.025) 1px, transparent 1px),
    linear-gradient(90deg, rgba(16,43,79,.025) 1px, transparent 1px),
    var(--cream);
  background-size: auto, 28px 28px, 28px 28px, auto;
  color: var(--navy-dark);
  line-height: 1.55;
}
a { color: var(--blue); text-underline-offset: 3px; }
.auth-shell { width: min(100%, 31rem); margin: 0 auto; }
.auth-shell.wide { width: min(100%, 42rem); }
.brand {
  width: max-content;
  margin: 0 auto 1.25rem;
  display: flex;
  align-items: center;
  gap: .65rem;
  color: var(--navy-dark);
  font-size: 1.05rem;
  font-weight: 800;
  letter-spacing: -.025em;
  text-decoration: none;
}
.brand-mark {
  width: 2rem;
  height: 2rem;
  display: grid;
  place-items: center;
  border-radius: .65rem .65rem .65rem .22rem;
  background: var(--navy);
  color: white;
  font-family: Georgia, serif;
  font-size: 1.05rem;
  font-style: italic;
}
.auth-card {
  padding: clamp(1.35rem, 5vw, 2.35rem);
  border: 1px solid var(--border);
  border-radius: 1.35rem;
  background: rgba(255,253,248,.97);
  box-shadow: 0 1.5rem 4.5rem rgba(24,42,63,.13);
}
.secure-label {
  margin: 0 0 .8rem;
  display: flex;
  align-items: center;
  gap: .45rem;
  color: var(--blue);
  font-size: .67rem;
  font-weight: 850;
  letter-spacing: .09em;
  text-transform: uppercase;
}
.secure-label::before {
  content: "";
  width: .45rem;
  height: .45rem;
  border-radius: 50%;
  background: var(--blue);
  box-shadow: 0 0 0 .25rem rgba(62,114,173,.11);
}
h1 { margin: 0 0 .45rem; font-size: clamp(1.7rem, 7vw, 2.25rem); line-height: 1.08; letter-spacing: -.045em; }
h2 { margin: 0; font-size: 1rem; }
p { margin: .65rem 0; }
.lede { margin: 0 0 1.35rem; color: var(--muted); font-size: .94rem; }
.client-pill {
  display: inline-block;
  padding: .1rem .45rem;
  border: 1px solid #c6d5e5;
  border-radius: 999px;
  background: #eef4fa;
  color: #234f7d;
  font-weight: 750;
}
.auth-progress {
  margin: 0 0 1.45rem;
  display: grid;
  grid-template-columns: auto 1fr auto 1fr auto;
  align-items: center;
  gap: .45rem;
  color: #929ba5;
  font-size: .66rem;
  font-weight: 750;
}
.auth-progress i { height: 1px; background: #d6d8d7; }
.auth-progress span { white-space: nowrap; }
.auth-progress .active { color: var(--blue); }
form { margin: 1rem 0 0; }
fieldset, .panel {
  min-width: 0;
  margin: 1rem 0;
  padding: 1rem;
  border: 1px solid var(--border);
  border-radius: .9rem;
  background: #fbf8f1;
}
legend {
  padding: 0 .35rem;
  color: #637083;
  font-size: .68rem;
  font-weight: 850;
  letter-spacing: .07em;
  text-transform: uppercase;
}
label { display: block; margin: .8rem 0 .28rem; color: #243a54; font-size: .78rem; font-weight: 750; }
input[type=text], input[type=email], input[type=password] {
  width: 100%;
  min-height: 2.85rem;
  padding: .7rem .8rem;
  border: 1px solid #c7c3bb;
  border-radius: .7rem;
  outline: 0;
  background: white;
  color: var(--navy-dark);
  font: inherit;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}
input:focus-visible { border-color: var(--blue); box-shadow: 0 0 0 .22rem rgba(62,114,173,.13); }
button, .button {
  min-height: 2.9rem;
  padding: .72rem 1rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--navy);
  border-radius: 999px;
  background: var(--navy);
  color: white;
  font: inherit;
  font-size: .82rem;
  font-weight: 800;
  text-decoration: none;
  cursor: pointer;
}
form > button[type=submit]:not(.secondary) { width: 100%; }
button:hover, .button:hover { background: var(--navy-dark); }
.secondary { min-height: 2.25rem; padding: .4rem .75rem; border-color: #c8c3ba; background: transparent; color: var(--navy); }
.secondary:hover { background: #f0ece3; }
.muted, .form-note { color: var(--muted); font-size: .78rem; }
.muted { margin-top: 1rem; text-align: center; }
.form-note {
  margin-top: 1rem;
  padding: .75rem .85rem;
  border-radius: .7rem;
  background: #edf3f9;
  color: #536b84;
}
.error { margin: 1rem 0; padding: .7rem .8rem; border: 1px solid #dfb2ae; border-radius: .7rem; background: #fff2f0; color: var(--danger); font-size: .82rem; }
.warn { margin: .9rem 0 0; padding: .75rem .8rem; border-left: 3px solid #b18b4d; border-radius: 0 .55rem .55rem 0; background: #f7f0e2; color: #695b45; font-size: .78rem; }
.account { padding: .75rem .85rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; border: 1px solid var(--border); border-radius: .75rem; background: #fbf8f1; font-size: .78rem; }
.account form { margin: 0; }
.choice { position: relative; margin: .45rem 0; }
.choice input { position: absolute; top: 50%; left: .8rem; margin: 0; transform: translateY(-50%); }
.choice label { margin: 0; padding: .7rem .75rem .7rem 2.15rem; border: 1px solid #d5d0c7; border-radius: .65rem; background: white; cursor: pointer; }
.choice input:checked + label { border-color: var(--blue); background: #edf4fb; box-shadow: 0 0 0 .18rem rgba(62,114,173,.10); }
.permission-list { margin: .7rem 0 0; padding: 0; display: grid; gap: .55rem; list-style: none; color: #576577; font-size: .8rem; }
.permission-list li { position: relative; padding-left: 1.35rem; }
.permission-list li::before { content: "✓"; position: absolute; left: 0; color: #4e7b50; font-weight: 900; }
.row { display: flex; gap: .7rem; flex-wrap: wrap; align-items: center; }
.success-mark {
  width: 3rem;
  height: 3rem;
  margin: 0 0 1rem;
  display: grid;
  place-items: center;
  border: 1px solid #75aa83;
  border-radius: 50%;
  background: #edf7ee;
  color: #367448;
  font-size: 1.35rem;
  font-weight: 900;
  box-shadow: 0 0 0 .4rem rgba(78,123,80,.08);
}
.return-button { width: 100%; }
.return-button[aria-disabled=true] { pointer-events: none; opacity: .48; }
.handoff-recovery {
  margin-top: 1.25rem;
  padding: .85rem;
  border: 1px solid var(--border);
  border-radius: .8rem;
  background: #fbf8f1;
  color: #536174;
  font-size: .78rem;
}
.handoff-recovery summary { color: var(--navy); font-weight: 800; cursor: pointer; }
.callback-copy { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .55rem; align-items: center; }
.callback-copy input { min-width: 0; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .7rem; }
.callback-copy .secondary { min-height: 2.85rem; background: white; }
.callback-copy .secondary:disabled { cursor: not-allowed; opacity: .5; }
.copy-status { min-height: 1.2em; margin: .5rem 0 0; color: #367448; font-weight: 700; }
code { padding: .1rem .3rem; border-radius: .3rem; background: #edf0f3; color: #384a5d; }
.auth-footer { margin: 1rem 0 0; color: #798492; font-size: .69rem; text-align: center; }
@media (max-width: 34rem) {
  body { padding-inline: .75rem; }
  .auth-card { border-radius: 1rem; }
  .auth-progress { grid-template-columns: auto 1fr auto; }
  .auth-progress span:nth-of-type(3), .auth-progress i:nth-of-type(2) { display: none; }
  .account { align-items: flex-start; flex-direction: column; }
  .account .secondary { width: auto; }
  .callback-copy { grid-template-columns: 1fr; }
  .callback-copy .secondary { width: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .001ms !important; }
}
"""


def page(
    title: str, content: str, *, wide: bool = False, context: str = "Secure account access"
) -> str:
    """Wrap trusted server-rendered page content in the shared Cottage browser shell."""
    width_class = "auth-shell wide" if wide else "auth-shell"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<main class="{width_class}">
  <a class="brand" href="/connect/"><span class="brand-mark" aria-hidden="true">C</span>Cottage</a>
  <section class="auth-card">
    <p class="secure-label">{html.escape(context)}</p>
    {content}
  </section>
  <p class="auth-footer">Secure OAuth · Your agents stay independently owned</p>
</main></body></html>"""
