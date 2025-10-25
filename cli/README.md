# Meet Telminator, your agent‑of‑agents.

Don’t pick tools. State your goal. Telminator discovers the best agents, connects them, and gets it done — across any framework, no lock‑in

try now [Telminator](https://app.telminator.com) 

# A2A CLI

The CLI is a small host application that demonstrates the capabilities of an `A2AClient`. It supports reading a server's `AgentCard` and text-based collaboration with a remote agent. All content received from the A2A server is printed to the console.

The client will use streaming if the server supports it.

## Prerequisites

- Python 3.12 or higher
- [UV](https://docs.astral.sh/uv/)
- A running A2A server

## Running the CLI

0. Clone the repo 
    ```bash
    git clone https://github.com/dil34556/Agent-CLI.git
    ```

1. Navigate to the CLI sample directory:

    ```bash
    cd cli
    ```

2. Run the example client

    ```sh
    uv run .
    ```
   
3. Reset config

    ```sh
    uv run . --reset-config
    ```

4. Run directly agent using url 
   ```sh
   uv run . --agent http://different-url:9000/
   ```

   for example `--agent https://sample-a2a-agent-908687846511.us-central1.run.app`. More command line options are documented in the source code.
   
## Disclaimer

Important: The sample code provided is for demonstration purposes and illustrates the mechanics of the Agent-to-Agent (A2A) protocol. When building production applications, it is critical to treat any agent operating outside of your direct control as a potentially untrusted entity.

All data received from an external agent—including but not limited to its AgentCard, messages, artifacts, and task statuses—should be handled as untrusted input. For example, a malicious agent could provide an AgentCard containing crafted data in its fields (e.g., description, name, skills.description). If this data is used without sanitization to construct prompts for a Large Language Model (LLM), it could expose your application to prompt injection attacks.  Failure to properly validate and sanitize this data before use can introduce security vulnerabilities into your application.

Developers are responsible for implementing appropriate security measures, such as input validation and secure handling of credentials to protect their systems and users.
