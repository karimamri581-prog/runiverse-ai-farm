name: Spatial Vision Agent
on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:

jobs:
  farm:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install Dependencies
        run: |
          pip install playwright pillow
          python -m playwright install --with-deps chromium
      - name: Run Vision Agent
        run: |
          python vision_agent.py "https://runiverseidle.com/forge" \
            --provider gemini
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GAME_EMAIL: "karimamri581@gmail.com"
          GAME_PASSWORD: "Taetae1997*"
      - name: Save Agent Memory
        if: always()
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git add vision_memory.json || echo "No memory file yet"
          git commit -m "Save Vision Agent Memory" || echo "No changes to commit"
          git push
