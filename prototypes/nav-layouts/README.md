# PROTOTYPE — 50 navigation / layout variants

**Throwaway.** This directory exists to answer one question: *where should the
navigation live, and what shape should it be?* It is not production code, it
does not talk to the backend, and nothing here is wired into the real app.

It lives on its own branch (`nav-layout-variants`) so it can't affect anything
you're testing on `week1-integration` or `master`.

## Run it

```bash
cd prototypes/nav-layouts
python -m http.server 8911
```

Then open **http://127.0.0.1:8911/** — or from your phone on the tailnet,
**https://owens-pc-vpn.tailff2683.ts.net:8911/** if you serve it there.

## Using it

- **← / →** or the dropdown — move between the 50 variants.
- **▦ All** — contact sheet of every variant; click one to jump to it.
- **☆** — favourite the current variant. Favourites persist in `localStorage`.
- **Copy favourites** (inside the All view) — copies your shortlist to the
  clipboard so you can paste it straight back to me.

Keyboard: `←` `→` to move, `f` to favourite, `a` for the All view, `Esc` to close.

Clicking a nav item inside a variant switches the mock page, so you can check
how each layout behaves as you move around rather than judging one static screen.

## What's mock and what's real

- **Real:** the five consolidated pages (Light / Audio / Automation / Rooms /
  System) and their sub-sections — these match what actually shipped.
- **Mock:** every value shown (82% brightness, 128 BPM, the history rows).
  Nothing fetches, nothing writes. Judge layout here, not data.

## The 50, by family

| # | Family | What's being explored |
|---|---|---|
| 1–10 | Side rails | Left/right, icon-only, collapsible, two-tier, floating, wide, dense, vertical text |
| 11–20 | Top nav | Tabs, sub-rows, pills, underline, segmented, three-zone, shrink-on-scroll, dropdowns, boxed, minimal |
| 21–26 | Bottom nav | Icon+label, icons only, floating pill, thumb arc, centre action button, control strip |
| 27–32, 44, 49 | Grid / launcher | App grid, bento, live previews, big buttons, springboard, card deck, zoom-out, widget board |
| 33–37 | Command-driven | Command palette, search-first, omnibar, quick switcher, keyboard-only |
| 38–43, 45–48, 50 | Spatial / content-led | Radial, carousel, accordion, folder tabs, master–detail, single-scroll, control-always-visible, focus mode, adaptive |

## How to give feedback

Favourite the ones worth pursuing, hit **Copy favourites**, and paste the list
back. Two or three is a useful answer; ten isn't. Worth saying separately:

- which one you'd want on **desktop**, and
- which one you'd want on **your phone** — they don't have to be the same, and
  #50 exists specifically because they might not be.

## When this is done

Fold the winning layout into `frontend/`, then delete this directory. The
variants aren't meant to be maintained — if this is still here in a month,
something went wrong.
