/* PROTOTYPE — throwaway. 50 navigation/layout variants for the Smart Bulb
   Dashboard's 5 consolidated pages (Light / Audio / Automation / Rooms /
   System). Layout exploration only: the content inside each variant is mock
   data, and nothing here talks to the real backend. See README.md. */

// Each variant declares a `family` (which structural skeleton to build) and a
// `css` class that restyles that skeleton. Grouping them this way is what makes
// 50 variants tractable — most differences are genuinely stylistic, and the
// ones that aren't get their own family.
window.VARIANTS = [
  // ---------------------------------------------------------- side rails --
  { n: 1,  name: "Classic Left Sidebar",        family: "sidebar", css: "v-classic",     note: "What ships today. Baseline to compare everything else against." },
  { n: 2,  name: "Right Sidebar",               family: "sidebar", css: "v-right",       note: "Same rail, mirrored. Suits right-handed phone/tablet reach." },
  { n: 3,  name: "Icon Rail (hover to expand)", family: "sidebar", css: "v-iconrail",    note: "Collapsed to glyphs; widens on hover. Maximum content width." },
  { n: 4,  name: "Collapsible Sidebar",         family: "sidebar", css: "v-collapsible", note: "Explicit pin/unpin toggle rather than hover — no accidental expansion." },
  { n: 5,  name: "Two-Tier Rail",               family: "sidebar", css: "v-twotier",     note: "Sub-tabs nested inside the rail instead of above the content." },
  { n: 6,  name: "Floating Card Rail",          family: "sidebar", css: "v-floating",    note: "Detached rounded panel with a shadow — nav reads as an object, not an edge." },
  { n: 7,  name: "Wide Rail + Descriptions",    family: "sidebar", css: "v-wide",        note: "Each entry gets a one-line description. Discoverable, costs width." },
  { n: 8,  name: "Rail with Inline Device",     family: "sidebar", css: "v-raildevice",  note: "Device picker moves into the rail, freeing the top bar entirely." },
  { n: 9,  name: "Compact Dense Rail",          family: "sidebar", css: "v-dense",       note: "Tighter rhythm, smaller type — more visible at once, less comfortable." },
  { n: 10, name: "Rail with Status Dots",       family: "sidebar", css: "v-raildots",    note: "Per-page activity dots (timer running, session live) right in the nav." },

  // ------------------------------------------------------------- top nav --
  { n: 11, name: "Top Horizontal Tabs",         family: "topnav",  css: "v-toptabs",     note: "Full-width tabs. Frees the entire left edge for content." },
  { n: 12, name: "Top Tabs + Sub-Row",          family: "topnav",  css: "v-topsub",      note: "Two stacked rows: pages on top, sub-tabs beneath." },
  { n: 13, name: "Centered Pills",              family: "topnav",  css: "v-pills",       note: "Rounded pill group, centered. Reads as a control, not a menu." },
  { n: 14, name: "Underline Indicator",         family: "topnav",  css: "v-underline",   note: "Classic underline-active tabs. Quiet and familiar." },
  { n: 15, name: "Segmented Control",           family: "topnav",  css: "v-segmented",   note: "iOS-style segmented switch. Best when pages feel like peers." },
  { n: 16, name: "Brand / Nav / Device",        family: "topnav",  css: "v-thirds",      note: "Three-zone top bar — brand left, nav centered, device right." },
  { n: 17, name: "Sticky Shrink-on-Scroll",     family: "topnav",  css: "v-shrink",      note: "Top bar condenses as you scroll. Reclaims vertical space." },
  { n: 18, name: "Top Nav + Dropdowns",         family: "topnav",  css: "v-dropdown",    note: "Sub-tabs live in hover dropdowns rather than a second row." },
  { n: 19, name: "Boxed Tabs",                  family: "topnav",  css: "v-boxed",       note: "Bordered tab boxes — heavier, but the active state is unmistakable." },
  { n: 20, name: "Minimal Text Nav",            family: "topnav",  css: "v-minimal",     note: "Plain text links, no chrome at all. Maximum restraint." },

  // ---------------------------------------------------------- bottom nav --
  { n: 21, name: "Bottom Bar (icon + label)",   family: "bottom",  css: "v-bottom",      note: "Phone-native. Everything within thumb reach." },
  { n: 22, name: "Bottom Bar (icons only)",     family: "bottom",  css: "v-bottomicon",  note: "Compact, but relies on icons being legible without labels." },
  { n: 23, name: "Floating Pill Bar",           family: "bottom",  css: "v-bottompill",  note: "Detached rounded bar hovering above the content." },
  { n: 24, name: "Bottom Bar + Top Title",      family: "bottom",  css: "v-bottomtitle", note: "Nav at the bottom, current page name at the top for orientation." },
  { n: 25, name: "Thumb Arc",                   family: "bottom",  css: "v-arc",         note: "Items curve along a natural thumb sweep instead of a straight line." },
  { n: 26, name: "Bottom + Center Action",      family: "bottom",  css: "v-fab",         note: "Raised center button for the single most common action (power)." },

  // ----------------------------------------------------- grid / launcher --
  { n: 27, name: "App Launcher Grid",           family: "grid",    css: "v-launcher",    note: "Home screen of big tiles; pick a page to enter it." },
  { n: 28, name: "Bento Box",                   family: "grid",    css: "v-bento",       note: "Mixed-size tiles — importance expressed as area." },
  { n: 29, name: "Live Preview Tiles",          family: "grid",    css: "v-preview",     note: "Each tile shows real state, so the home screen is useful by itself." },
  { n: 30, name: "Big Button 2×3",              family: "grid",    css: "v-bigbtn",      note: "Huge targets. Wall-tablet / across-the-room friendly." },
  { n: 31, name: "Springboard Pages",           family: "grid",    css: "v-springboard", note: "Paged icon grid with dots, iOS-home style." },
  { n: 32, name: "Card Deck",                   family: "grid",    css: "v-deck",        note: "Overlapping cards that fan out on hover." },

  // ------------------------------------------------------ command-driven --
  { n: 33, name: "Command Palette First",       family: "command", css: "v-palette",     note: "⌘K is the primary nav; the sidebar is gone entirely." },
  { n: 34, name: "Search-First",                family: "command", css: "v-search",      note: "A persistent search field replaces browsing." },
  { n: 35, name: "Omnibar",                     family: "command", css: "v-omnibar",     note: "One top field for both navigation and direct commands." },
  { n: 36, name: "Quick Switcher Overlay",      family: "command", css: "v-switcher",    note: "Hold a key to overlay a switcher, release to jump." },
  { n: 37, name: "Keyboard-Driven",             family: "command", css: "v-keys",        note: "Single-key jumps with visible hint badges. No pointer needed." },

  // ------------------------------------------------- spatial / unusual --
  { n: 38, name: "Radial Menu",                 family: "radial",  css: "v-radial",      note: "Pages arranged in a circle around a center control." },
  { n: 39, name: "Vertical Text Rail",          family: "sidebar", css: "v-vertical",    note: "Rotated labels in a very narrow rail. Distinctive, costs legibility." },
  { n: 40, name: "Swipe Carousel",              family: "carousel",css: "v-carousel",    note: "Pages sit side by side; swipe between them. No nav chrome." },
  { n: 41, name: "Accordion Column",            family: "accordion",css:"v-accordion",   note: "Every page stacked and collapsed; expand what you need." },
  { n: 42, name: "File Folder Tabs",            family: "topnav",  css: "v-folder",      note: "Physical folder-tab metaphor with overlapping edges." },
  { n: 43, name: "Master–Detail Split",         family: "split",   css: "v-split",       note: "Nav list and content side by side, both scrollable." },
  { n: 44, name: "Zoom-Out Overview",           family: "grid",    css: "v-zoom",        note: "Zoom out to see all pages at once, click one to zoom in." },

  // -------------------------------------------------------- content-led --
  { n: 45, name: "Single Scroll + Sticky Nav",  family: "scroll",  css: "v-scroll",      note: "Everything on one page; nav scroll-spies the current section." },
  { n: 46, name: "Control Always Visible",      family: "split",   css: "v-controlfirst",note: "Power/brightness pinned permanently; everything else swaps beside it." },
  { n: 47, name: "Persistent Control Strip",    family: "bottom",  css: "v-strip",       note: "Thin always-on control strip docked under the content." },
  { n: 48, name: "Focus Mode",                  family: "topnav",  css: "v-focus",       note: "One thing at a time, nav hidden until summoned. Minimal chrome." },
  { n: 49, name: "Widget Board",                family: "grid",    css: "v-widgets",     note: "Draggable cards you arrange yourself — no fixed nav at all." },
  { n: 50, name: "Adaptive (rail ⇄ bottom)",    family: "sidebar", css: "v-adaptive",    note: "Sidebar on desktop, bottom bar under 700px. Resize to see it flip." },
];

// The five consolidated pages, with the sub-sections each one owns.
window.PAGES = [
  { id: "light",      label: "Light",      icon: "◐", subs: ["Control", "Scenes & Effects", "Presets"] },
  { id: "audio",      label: "Audio",      icon: "◎", subs: ["Live Session", "Session Presets"] },
  { id: "automation", label: "Automation", icon: "◷", subs: ["Timers", "Schedule"] },
  { id: "rooms",      label: "Rooms",      icon: "▦", subs: ["Groups", "Zones"] },
  { id: "system",     label: "System",     icon: "⚙", subs: ["History", "Diagnostics", "Settings"] },
];
