# Persistent Audio Studio and Expo Companion

## Problem Statement

The dashboard feels less responsive than it should, and audio controls do not
reliably retain their configuration. Saved sessions omit newer signal settings
and the capture-source kind. The user wants a trustworthy, adjustable lighting
mixer that listens to CABLE Output, a polished web interface, and an easy phone app
without operating a second application container.

## Solution

Improve the existing working dashboard in four verified stages: persistent live
mixing, reliable CABLE capture and measured processing, an animated web studio,
and a native Expo Go companion sharing the same backend and saved settings.
Retain existing lighting features and private remote access throughout.

## User Stories

1. As the owner, I want the existing bulb connection to keep working during upgrades.
2. As a listener, I want CABLE Output selected by name and audio API so device-index changes do not select another input.
3. As a listener, I want capture to reconnect after the backend restarts.
4. As a listener, I want clear disconnected, silent, receiving, and running states.
5. As a listener, I want a lighting mixer with frequency-band gains and master sensitivity.
6. As a listener, I want to adjust the noise gate and gain control while listening.
7. As a listener, I want smoothing and brightness limits I can adjust live.
8. As a listener, I want all supported mixer settings saved after adjustment.
9. As a listener, I want settings restored after a browser, app, or backend restart.
10. As a listener, I want settings saved even before starting an audio session.
11. As a listener, I want named session presets that recall the complete lighting mix.
12. As a listener, I want to update or rename my presets without recreating them.
13. As a listener, I want an explicit save failure instead of a false Saved label.
14. As a listener, I want rapid slider changes to converge on my latest choice.
15. As a listener, I want a slow bulb to leave analysis and controls responsive.
16. As a listener, I want latency measurements that distinguish processing from bulb communication.
17. As a listener, I want visible level and spectrum meters showing actual captured audio.
18. As the owner, I want preserved flash limits and an immediate stop control.
19. As a web user, I want a dark charcoal and amber studio with readable controls.
20. As a web user, I want smooth meter, control, and page animations that respect reduced motion.
21. As a touch or keyboard user, I want every control to work without hovering.
22. As a phone user, I want native light, scene, audio, and preset controls in Expo Go.
23. As a phone user, I want the phone and web dashboard to use the same saved settings.
24. As a phone user, I want a connection screen that remembers the backend and explains connection failures.
25. As a phone user, I want a QR code that loads this app's actual bundle.
26. As the owner, I want the API, web dashboard, and Expo bundle server managed in one Podman container.
27. As the owner, I want saved settings to survive replacement of that container.
28. As the owner, I want measured before/after results and a repeatable recovery procedure.

## Implementation Decisions

- Evolve the existing FastAPI, vanilla-JavaScript frontend, and audio engine;
  avoid a speculative backend rewrite or a web-framework migration.
- Use one complete, validated session-settings contract for live edits, saved
  defaults, named presets, and resume. Read existing saved records compatibly.
- Store per-bulb settings on the existing persistent data volume. Use atomic
  writes, preserve old data on invalid input, and expose save failures.
- Maintain one capture-source identity independent of volatile PortAudio indices.
  Prefer the installed WASAPI CABLE Output endpoint; do not silently fall back
  to a microphone when that selected source disappears.
- Keep Windows capture in the existing host bridge. The Linux container handles
  API, analysis, web assets, and the Expo development server; the phone runs Expo Go.
- Use bounded latest-value queues between capture, transport, and bulb dispatch.
  Preserve the rule that capture callbacks never perform bulb network I/O.
- Make adjustments update active sessions without restarting the source where
  possible. Separate draft, pending, saved, and failed UI states.
- Use persistent client-side controls with targeted telemetry updates rather
  than rebuilding focused controls on each status refresh.
- Share backend API contracts and saved state between native and web controls.
  Use native Expo screens for the everyday lighting and audio workflow; retain
  access to the full web dashboard for existing administration features.
- Retain charcoal/amber design tokens, strengthen hierarchy and control affordances,
  animate transforms/opacity and meaningful live meters, and respect reduced motion.
- Package Metro and API processes under a supervised container entry point with
  graceful shutdown. Publish the phone-reachable bundle URL explicitly and verify
  its app identity and bundle contents against the fleet registry.
- Keep current credentials private, preserve the current PIN configuration, and
  do not introduce public exposure or automatic cloud key retrieval.

## Testing Decisions

- Test user-visible behavior at the existing REST API and rendered app boundaries:
  edit, read back, restart/reload, recall preset, and compare effective settings.
- Use existing fake bulb and synthetic audio fixtures for deterministic regression
  tests. Keep every disk-backed state file isolated from live machine data.
- Test invalid writes, unavailable sources, reconnects, out-of-order responses,
  stale queued updates, and save failures through public interfaces.
- Measure processing against fixed known signals and report environment and timing
  percentiles. Compare equivalent before/after workloads; do not claim an arbitrary
  end-to-end latency below the physical bulb's measured response time.
- Verify a real Windows CABLE stream reaches the container and changes telemetry.
  Exercise real local bulb control with a bounded, non-flashing test and leave the
  bulb off after verification unless the user directs otherwise.
- Exercise web journeys with real Chromium at desktop and phone widths, including
  keyboard focus, reduced motion, rapid edits, saved reload, and offline recovery.
- Typecheck and test Expo controls, retrieve its real manifest and bundle, and
  validate rendering on an available device/emulator. Distinguish bundle checks
  from actual phone interaction if physical verification requires the user.
- Re-run the full relevant suite and required checks at the end; review the diff,
  verify the deployed container, and retain evidence in the iteration report.

## Out of Scope

- Windows per-application playback-volume mixing, audio recording, or streaming
  captured audio to the phone.
- Bluetooth support, new hardware, cloud-dependent lighting control, and app-store publication.
- Automatic lighting start at boot; saved settings restore, but starting a session remains intentional.
- Guaranteeing zero latency or treating synthetic tests as proof of physical output.
- Completing unrelated historical roadmap items.

## Further Notes

Owen approved the visual direction, lighting-mixer scope, four build stages, and
creation of this spec and tickets followed by implementation. GitHub is this
project's configured tracker. Existing roadmap umbrella issues remain unchanged.
Expo Go compatibility and authentication requirements must be checked against the
installed phone/runtime version before final deployment.

## Approved mobile refinement — September 13, 2026

Owen requested a searchable future multi-bulb dropdown, a real color wheel with
visible named swatches, simpler Windows input selection, a single Audio area
instead of separate Mixer/Mixes navigation, useful ready-made and complete saved
mixes, and access to the full dashboard from the phone. Native core controls
cover those daily actions. Advanced dashboard views are hosted within the same
Expo app through WebView; this preserves the complete existing workflows.
Verification includes isolated API/browser journeys across two bulbs, favorite
application, initial connection failure recovery, input identity, saved mixes,
nullable limits, and device handoff/reload in dashboard tools. Native phone
WebView rendering remains a separate device-level check.
