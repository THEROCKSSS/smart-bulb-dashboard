# 007 — Persistent Audio Studio and Expo companion

## Goal and implementation

Issues #84–#87 implement spec #83: a complete, persistent lighting mix, reliable
CABLE Output capture, a responsive studio UI and a native phone companion served
from the same Podman container as the API.

`audio_settings.py` owns validation and default values. Atomic per-bulb writes
retain the previous complete record on failure. Presets, resume and scheduled
presets use the complete contract. Web and phone share the ordered write queue;
stale polls cannot overwrite a later local edit. Meters update transforms rather
than replacing the whole meter DOM. Polling has one request in flight and pauses
when hidden. Native controls include scenes and support the existing optional PIN.

## Failures found and fixed

- Three gains were ignored by modes with more than three bands. They now map as
  low/middle/high anchors, with cached mappings and a zero-work unity path.
- Preset recall dropped null values, preventing an unlimited mix from clearing a
  prior duration. Stored presets now preserve nulls and normalize legacy records.
- Gentle mode persisted a flash-heavy mode that Start rejected. The effective
  ambient mode is now saved. Genre presets also refresh the client queue.
- Native brightness initially used the wrong API field. It now uses the current
  color/white percentage, refreshes desktop changes and guards device switches.
- Deduplicated device enumeration discarded saved MME identities. Resolution now
  uses raw enumeration. Stereo capacity recovers after choosing a mono source.
- CABLE playback failed with WASAPI, DirectSound and MME. Read-only Voicemeeter
  inspection found A2 already owned CABLE Input but desktop/aux strips did not send
  there. Enabling their A2 sends fixed the path without changing speaker routing.
- Test isolation initially omitted the lightshow directory. Three secret-scanner
  tests also traversed to the parent repository from ignored test temp folders.
  Creating the isolated directory and setting `GIT_CEILING_DIRECTORIES` fixed the
  test harness; planted-secret detection then passed.

## Mobile feedback refinement

Owen found the initial native surface too limited. The revised app merges Mixer
and Mixes into Audio, adds a real SVG color wheel, four named color previews,
full preset/favorite colors, white temperature, a remembered bulb dropdown and
searchable Windows capture dropdown. Audio now includes the web genre presets,
mode explanations, complete saved mixes and advanced configuration. Tools hosts
the existing dashboard inside a native WebView; browser builds navigate to it.

Review caught an incorrect RGB favorite endpoint, address recovery disabled when
the old server was unreachable, stale command poll responses, a device inventory
that did not refresh on returning from administration, and launch query parameters
that overrode later device selections on reload. All were corrected. Browser
assertions use the visible swatch selection marker because React Native Web does
not render selected button state as an aria-selected attribute.

## Evidence

After the mobile feedback refinement, the full suite returned **760 passed in
114.36 seconds** and all **five queue tests passed**. The expanded journeys cover
including first-connection recovery, wheel selection, visible swatches, two-bulb
routing, favorite colors, Windows input identity, starting/saved mixes, clearing
a duration, and dashboard-tool device handoff/reload. Final counts are in HANDOFF.

The final audio-engine CABLE test processed 1,021 frames, with peak 0.01523 and
RMS up to 0.00649. The bulb sender completed 52 sends, with no errors or pipeline
frame drops; 16.55% of capture frames exceeded the late threshold. Software p50
was 6.332 ms, with summed stage p95 20.950 ms. Bulb send p50 was 12.321 ms,
p95 80.446 ms and worst 112.640 ms. These are a short local run,
not universal latency guarantees. The final verified state was online and OFF.

Fixed CPU workload: Python 3.11.15, NumPy 2.4.6, Windows, 20,000 frames of 1,024
samples at 44.1 kHz, mixed 60/1,000/8,000 Hz tones. Alternating baseline/current
runs produced p50 0.2018/0.1882 ms and 0.1866/0.1917 ms. CPU analysis is broadly
unchanged within host noise; no speedup claim is made. The first uncached mapping
was slower and was replaced before final release. The transport now makes one
write per frame instead of two; queues stay bounded and discard stale audio.

Expo SDK 57 Android, iOS and web exports passed. The running manifest identifies
`smart-bulb-studio`, advertises port 8106, and its actual bundle contains the native
mixer and presets. Fleet reports the Smart Bulb row `ok`. API and Expo run under
one container supervisor. Physical phone interaction remains to be confirmed.

## Reproduce

```powershell
$env:GIT_CEILING_DIRECTORIES=(Get-Location).Path
$env:SBD_BROWSER_TEST='1'
backend/venv/Scripts/python.exe -B -m pytest backend/tests cli/tests -q -p no:cacheprovider --basetemp=.state/verification-tests
node --test frontend/tests/mixer.test.cjs
backend/venv/Scripts/python.exe -B tools/benchmark-audio.py --iterations 20000
```

In `mobile/`, run `npm ci`, `npx tsc --noEmit`, then
`npx expo export --platform web --platform android --platform ios` before the
browser tests. See `docs/audio-studio.md` for deployment and routing.

## Known verification limits

Native Android/iOS exports and browser control journeys pass. The WebView is
included in Expo Go, but its native rendering, PIN-cookie handoff and native file
download behavior have not been exercised on Owen's physical phone. CPU analysis
is roughly unchanged; the main improvements are input routing, bounded queues,
persistence, interaction feedback and protection from stale updates. The npm
audit reports 10 moderate findings propagated from the uuid/xcode build-tool
chain, with no high or critical findings. Its suggested automatic major-version
fix downgrades Expo incompatibly; no forced downgrade was applied.
