# rembgGUI desktop design

The desktop shell is a focused, timeline-first editor: compact source identity,
one shared Original/Result stage, timeline space, a continuous inspector rail,
and fixed preview/render actions. It deliberately avoids dashboard cards and
decorative panels.

- Default window: 1440×900; hard minimum: 1100×720.
- UI font: IBM Plex Sans; technical values use IBM Plex Mono, with platform
  system-font fallback if packaged assets are unavailable.
- Background `#111315`; canvas `#0B0D0F`; inspector `#171A1D`; controls
  `#202428`; text `#F3F5F7`; secondary `#A3ABB2`; disabled `#687078`;
  divider `#30353A`; focus/accent `#B7F34A`; hover `#C8FF63`.
- Semantic status colors are error `#FF6B6B`, warning `#F3B849`, success
  `#63D69A`; result transparency uses `#252A2E` / `#343A3F` checkerboard.
- Spacing uses 4/8/12/16/24/32 px. Controls use 4 px radii; primary controls
  are at least 40 px high. Hover/focus color feedback takes 120 ms; result and
  disclosure updates are immediate.
