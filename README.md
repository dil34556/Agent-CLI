# Agent CLI
 
# .env.example
AGENT_URL=http://127.0.0.1:10002/
X_API_KEY=your_api_key_here

# First run - will prompt for setup
uv run .

# After setup - just run
uv run .

# Reset config
uv run . --reset-config

# Override with different settings
uv run . --agent http://different-url:9000/
