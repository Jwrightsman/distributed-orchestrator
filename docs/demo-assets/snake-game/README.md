# Demo asset — snake-game

**Task:** Build a retro Snake game as ONE single self-contained HTML file with all CSS and JavaScript inline in that file. Dark background with neon-glow styling, a live score display, arrow-key controls, collision detection, and a game-over screen with a restart button. The final deliverable must be one complete HTML document starting with <!DOCTYPE html> that runs by double-clicking the file — no external files, no frameworks, no image assets (draw everything on a <canvas>). REQUIRED BEHAVIOUR ON LOAD: the snake must already be moving the moment the page opens — no start screen, no title screen, no 'press any key' prompt, and no click required to begin. There must be exactly ONE overlay element, used only for game over; it starts with style="display:none" in the HTML, is shown only when the snake dies, and is hidden again by the restart button. The text 'GAME OVER' must never be visible before the snake has died.

- Real run `20260808_223452`, captured 2026-08-08T22:44:43+00:00
- Model `qwen3.5:4b` · prompt set `v3` · rating PASS · 4 subtasks
- Mechanical check: clean

Captured verbatim from a real pipeline run — nothing here was written or edited by hand. Use it on camera if live inference misbehaves.

## Files

- `code/index.html`

`transcript/` holds the 4 builder outputs this was assembled from.

**Note:** Verified playable in headless Chromium: canvas draws, game loop animates, arrow keys steer, no JS errors. From the 10-run reliability set (scripts/showcase_results) where only 2/10 were playable — this is one of the two. Prompt set v3, qwen3.5:4b.
