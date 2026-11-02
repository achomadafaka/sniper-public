## World Map Usability Pass

### Live defects observed

1. The world map ignores the active status/region filters.
   - The event list updates, but the map keeps rendering the full event set.
2. Default label density is too high.
   - Dense regions like Europe and the Middle East collapse into unreadable text blocks.
3. Zoom currently increases label clutter.
   - At higher zoom levels the map adds extra labels instead of improving readability.
4. Mobile is not recoverable.
   - Mobile disables zoom entirely, so dense clusters cannot be inspected.

### Design goals

1. The map and event list must always reflect the same filtered event set.
2. Labels should be sparse by default and become more informative only when there is room.
3. Region/status filtering should improve the map, not just the list.
4. Mobile must offer an explicit way to inspect dense clusters.

### Implementation plan

1. Make filtered events the map input.
   - Feed `filtered` events from `intelligence._renderFilteredEvents()` into `WorldMap`.
   - Remove the stale “render all events once, then only filter cards” behavior.
2. Replace the current label strategy with a priority-based layout.
   - Prioritize idea-backed and escalating events.
   - Use coarser collision buckets on mobile.
   - Reduce default labels per bucket.
   - Remove the current “add more labels at zoom >= 2” behavior.
3. Add map controls and view fitting.
   - Add zoom-in, zoom-out, and reset controls.
   - When the filtered set is a subset, auto-fit the map to those events.
4. Improve mobile interaction.
   - Enable D3 zoom/pan on mobile.
   - Keep labels even sparser on small screens.
5. Add focused tests.
   - Unit-test the label prioritization / layout helper.
   - Unit-test focus transform generation.
   - Add a lightweight regression check that the frontend now passes filtered events to the map.

### Verification

1. Desktop screenshots:
   - default view
   - region-filtered view
   - zoomed cluster view
2. Mobile screenshot:
   - default filtered view
3. Automated:
   - focused tests for label layout and map fitting
