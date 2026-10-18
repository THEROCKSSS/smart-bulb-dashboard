# Smart Bulb Dashboard

Local control of smart lights, including lighting that responds to captured audio.

## Language

**Lighting mixer**:
Controls that shape how captured audio drives the lights, including frequency-band
gains, sensitivity, noise gate, smoothing, and brightness limits. This is distinct
from changing the playback volume of Windows applications.

**Capture source**:
The audio input supplying a lighting session. Owen's selected source is CABLE Output
(VB-Audio Virtual Cable), which carries audio routed into the virtual cable.

**Session**:
A running audio-reactive lighting experience for a bulb or group, using a capture
source, mode, and lighting-mixer settings.

**Session preset**:
A named set of session settings that can be recalled and adjusted again later.
It includes the lighting mix, rather than only a selection of colors.

**Mode**:
The behavior that translates audio features into changes in light color and brightness.

**Dwell**:
The minimum time between successive lighting changes sent to a bulb.
