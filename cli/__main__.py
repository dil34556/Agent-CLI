import asyncio
import base64
import os
from pathlib import Path
import urllib
import warnings
from uuid import uuid4

import asyncclick as click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown
from rich.table import Table
from rich.live import Live
from rich.spinner import Spinner
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.json import JSON
from rich import box
from dotenv import load_dotenv, set_key

from a2a.client import A2ACardResolver, A2AClient
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.types import (
    FilePart,
    FileWithBytes,
    GetTaskRequest,
    JSONRPCErrorResponse,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    Part,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskQueryParams,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

# Suppress deprecation warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

console = Console()

# Define .env file path
ENV_FILE = Path(__file__).parent / '.env'

# Load existing .env if it exists
load_dotenv(ENV_FILE)


@click.command()
@click.option('--agent', default=None, help='Agent URL')
@click.option(
    '--bearer-token',
    help='Bearer token for authentication',
    envvar='A2A_CLI_BEARER_TOKEN',
)
@click.option('--api-key', default=None, help='API Key for X-API-Key header', envvar='API KEY')
@click.option('--session', default=0, help='Session ID (0 for new session)')
@click.option('--history', default=False, is_flag=True, help='Show conversation history')
@click.option('--use_push_notifications', default=False, is_flag=True, help='Enable push notifications')
@click.option('--push_notification_receiver', default='http://localhost:5000', help='Push notification receiver URL')
@click.option('--header', multiple=True, help='Additional headers (format: key=value)')
@click.option(
    '--enabled_extensions',
    default='',
    help='Comma-separated list of extension URIs',
)
@click.option('--debug', default=False, is_flag=True, help='Show debug information')
@click.option('--reset-config', default=False, is_flag=True, help='Reset saved configuration')
async def cli(
    agent,
    bearer_token,
    api_key,
    session,
    history,
    use_push_notifications: bool,
    push_notification_receiver: str,
    header,
    enabled_extensions,
    debug,
    reset_config,
):
    """A2A Agent CLI - Interactive agent communication interface"""
    
    # Print welcome banner
    console.print(Panel.fit(
        "[bold cyan]A2A Agent CLI[/bold cyan]\n"
        "[dim]Interactive Agent-to-Agent Communication[/dim]",
        border_style="cyan"
    ))
    
    # Handle reset config
    if reset_config:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
            console.print("[bold green]✓[/bold green] Configuration reset!")
        else:
            console.print("[yellow]No configuration to reset[/yellow]")
        return
    
    # Check if .env exists, if not do first-time setup
    if not ENV_FILE.exists() or not os.getenv('AGENT_URL') or not os.getenv('X_API_KEY'):
        console.print("\n[bold yellow]🎉 First Time Setup[/bold yellow]\n")
        
        # Ask for Agent URL
        if not agent:
            agent = Prompt.ask(
                "[bold cyan]Agent URL[/bold cyan]",
                default="http://127.0.0.1:10000/"
            )
        
        # Ask for API Key
        if not api_key:
            api_key = Prompt.ask(
                "[bold cyan]API-Key[/bold cyan]",
                password=True
            )
        
        # Save to .env
        ENV_FILE.touch(exist_ok=True)
        set_key(ENV_FILE, 'AGENT_URL', agent)
        set_key(ENV_FILE, 'X_API_KEY', api_key)
        
        console.print("\n[bold green]✓[/bold green] Configuration saved to .env!")
        console.print("[dim]Next time just run: uv run .[/dim]\n")
    
    # Load from environment if not provided via CLI
    if not agent:
        agent = os.getenv('AGENT_URL', 'http://localhost:8083')
    
    if not api_key:
        api_key = os.getenv('X_API_KEY')
    
    # Build headers
    headers = {h.split('=')[0]: h.split('=')[1] for h in header}
    
    if api_key:
        headers['X-API-Key'] = api_key
    
    if bearer_token:
        headers['Authorization'] = f'Bearer {bearer_token}'

    if enabled_extensions:
        ext_list = [ext.strip() for ext in enabled_extensions.split(',') if ext.strip()]
        if ext_list:
            headers[HTTP_EXTENSION_HEADER] = ', '.join(ext_list)
    
    async with httpx.AsyncClient(timeout=30, headers=headers) as httpx_client:
        # First attempt to connect without auth
        with console.status("[bold green]Connecting to agent...", spinner="dots"):
            try:
                card_resolver = A2ACardResolver(httpx_client, agent , agent_card_path="/.well-known/agent.json")
                card = await card_resolver.get_agent_card()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in [401, 403]:
                    # Authentication required
                    console.print("\n[bold yellow]⚠[/bold yellow] Authentication required")
                    
                    # Ask for authentication
                    auth_choice = Prompt.ask(
                        "[cyan]Authentication type[/cyan]",
                        choices=["api-key", "bearer-token"],
                        default="api-key"
                    )
                    
                    if auth_choice == "api-key":
                        header_name = Prompt.ask("[cyan]Header name[/cyan]", default="X-API-Key")
                        api_key = Prompt.ask(f"[cyan]{header_name}[/cyan]", password=True)
                        headers[header_name] = api_key
                    else:
                        token = Prompt.ask("[cyan]Bearer token[/cyan]", password=True)
                        headers['Authorization'] = f'Bearer {token}'
                    
                    # Retry with auth
                    httpx_client.headers.update(headers)
                    with console.status("[bold green]Reconnecting with authentication...", spinner="dots"):
                        try:
                            card_resolver = A2ACardResolver(httpx_client, agent , agent_card_path="/.well-known/agent.json")
                            card = await card_resolver.get_agent_card()
                        except Exception as e2:
                            console.print(f"\n[bold red]✗ Authentication failed:[/bold red] {e2}")
                            return
                else:
                    console.print(f"\n[bold red]✗ Failed to connect to agent:[/bold red] {e}")
                    return
            except Exception as e:
                console.print(f"\n[bold red]✗ Failed to connect to agent:[/bold red] {e}")
                return

        console.print("[bold green]✓[/bold green] Connected to agent!\n")
        
        # Display agent card in a nice format
        agent_table = Table(show_header=False, box=box.ROUNDED, border_style="green", title="[bold]Agent Information[/bold]", title_style="bold white")
        agent_table.add_column("Property", style="cyan", no_wrap=True, width=20)
        agent_table.add_column("Value", style="white")
        
        agent_table.add_row("Name", card.name or "Unknown")
        if card.description:
            agent_table.add_row("Description", card.description)
        agent_table.add_row("Version", card.version or "N/A")
        agent_table.add_row("Streaming", "✓ Enabled" if card.capabilities.streaming else "✗ Disabled")
        agent_table.add_row("Push Notifications", "✓ Supported" if card.capabilities.push_notifications else "✗ Not Supported")
        
        console.print(agent_table)

        if debug and card.skills:
            console.print("\n[bold]Available Skills:[/bold]")
            for skill in card.skills:
                console.print(f"  [cyan]•[/cyan] {skill.name}: [dim]{skill.description}[/dim]")

        if debug and headers:
            console.print("\n[bold]Active Headers:[/bold]")
            header_table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
            header_table.add_column("Header", style="cyan")
            header_table.add_column("Value", style="yellow")
            for key, value in headers.items():
                # Mask sensitive tokens
                display_value = value if 'token' not in key.lower() and 'key' not in key.lower() else f"{value[:8]}...{value[-4:]}" if len(value) > 12 else "***"
                header_table.add_row(key, display_value)
            console.print(header_table)

        notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
        notification_receiver_host = notif_receiver_parsed.hostname
        notification_receiver_port = notif_receiver_parsed.port

        if use_push_notifications:
            from hosts.cli.push_notification_listener import PushNotificationListener
            
            with console.status("[bold yellow]Starting push notification listener..."):
                push_notification_listener = PushNotificationListener(
                    host=notification_receiver_host,
                    port=notification_receiver_port,
                )
                push_notification_listener.start()
            console.print("[bold green]✓[/bold green] Push notifications enabled")

        client = A2AClient(httpx_client, agent_card=card)
        continue_loop = True
        streaming = card.capabilities.streaming
        context_id = session if session > 0 else uuid4().hex
        
        console.print(f"\n[dim]Session: {context_id[:16]}...[/dim]")
        console.print("[dim]Type 'exit', 'quit', 'ext', or 'qt' to end the session[/dim]")
        console.print()

        while continue_loop:
            continue_loop, _, task_id = await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                None,
                context_id,
                debug,
            )

            if history and continue_loop and task_id:
                console.print("\n[bold cyan]━━━ Conversation History ━━━[/bold cyan]")
                task_response = await client.get_task({'id': task_id, 'historyLength': 10})
                
                if hasattr(task_response.root, 'result') and hasattr(task_response.root.result, 'history'):
                    for idx, msg in enumerate(task_response.root.result.history, 1):
                        role_color = "blue" if msg.role == "user" else "green"
                        role_label = "You" if msg.role == "user" else "Agent"
                        console.print(f"\n[bold {role_color}]{role_label}:[/bold {role_color}]")
                        for part in msg.parts:
                            if hasattr(part, 'text'):
                                console.print(f"  {part.text}")


def extract_text_from_parts(parts):
    """Extract text from various part structures"""
    texts = []
    if not parts:
        return texts
    
    for part in parts:
        if hasattr(part, 'text'):
            texts.append(part.text)
        elif hasattr(part, 'root') and hasattr(part.root, 'text'):
            texts.append(part.root.text)
        elif isinstance(part, dict):
            if 'text' in part:
                texts.append(part['text'])
            elif 'kind' in part and part['kind'] == 'text' and 'text' in part:
                texts.append(part['text'])
    
    return texts


async def completeTask(
    client: A2AClient,
    streaming,
    use_push_notifications: bool,
    notification_receiver_host: str,
    notification_receiver_port: int,
    task_id,
    context_id,
    debug=False,
):
    prompt = Prompt.ask(
        "[bold blue]You[/bold blue]",
        default=""
    )
    
    # Extended exit commands
    if not prompt or prompt.lower() in ['quit', 'exit', ':q', 'q', 'ext', 'qt']:
        console.print("\n[bold yellow]👋 Goodbye![/bold yellow]")
        return False, None, None

    # Strip extra whitespace
    prompt = prompt.strip()

    message = Message(
        role='user',
        parts=[TextPart(text=prompt)],
        message_id=str(uuid4()),
        task_id=task_id,
        context_id=context_id,
    )

    payload = MessageSendParams(
        id=str(uuid4()),
        message=message,
        configuration=MessageSendConfiguration(accepted_output_modes=['text']),
    )

    if use_push_notifications:
        payload['pushNotification'] = {
            'url': f'http://{notification_receiver_host}:{notification_receiver_port}/notify',
            'authentication': {'schemes': ['bearer']},
        }

    taskResult = None
    response_message = None
    task_completed = False
    agent_responded = False
    status_messages = []  # Store intermediate status messages
    final_artifact_shown = False
    
    if streaming:
        console.print()
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            progress_task = progress.add_task("[cyan]Thinking...", total=None)
            
            try:
                response_stream = client.send_message_streaming(
                    SendStreamingMessageRequest(id=str(uuid4()), params=payload)
                )
                
                async for result in response_stream:
                    if debug:
                        console.print(f"[dim]stream event => {result.root}[/dim]")
                    
                    if isinstance(result.root, JSONRPCErrorResponse):
                        progress.stop()
                        console.print(f"[bold red]✗ Error:[/bold red] {result.root.error}")
                        if debug:
                            console.print(f"[dim]Context: {context_id}, Task: {task_id}[/dim]")
                        return False, context_id, task_id
                    
                    event = result.root.result
                    
                    # Extract context_id from event
                    if hasattr(event, 'context_id'):
                        context_id = event.context_id
                    elif hasattr(event, 'contextId'):
                        context_id = event.contextId
                    
                    if isinstance(event, Task):
                        task_id = event.id
                        if debug:
                            progress.update(progress_task, description=f"[cyan]Task: {task_id[:8]}...")
                    
                    elif isinstance(event, TaskStatusUpdateEvent):
                        if hasattr(event, 'task_id'):
                            task_id = event.task_id
                        elif hasattr(event, 'taskId'):
                            task_id = event.taskId
                        
                        # Check status state
                        status_state = event.status.state if hasattr(event.status, 'state') else 'unknown'
                        
                        if debug:
                            progress.update(progress_task, description=f"[cyan]Status: {status_state}")
                        
                        # Show intermediate status messages (working state)
                        if status_state == 'working' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts and not agent_responded:
                                progress.stop()
                                console.print("[bold yellow]Agent:[/bold yellow]")
                                agent_responded = True
                            
                            for text in texts:
                                console.print(f"[dim]{text}[/dim]")
                                status_messages.append(text)
                        
                        # Check for final message in input-required state
                        elif status_state == 'input-required' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts:
                                if not agent_responded:
                                    progress.stop()
                                    console.print("[bold green]Agent:[/bold green]")
                                    agent_responded = True
                                
                                for text in texts:
                                    console.print(text)
                                console.print()
                        
                        # Check if task is completed
                        if status_state == 'completed':
                            task_completed = True
                            if not agent_responded:
                                progress.stop()
                    
                    elif isinstance(event, TaskArtifactUpdateEvent):
                        # Handle artifact updates - this is where the final answer is!
                        if hasattr(event, 'task_id'):
                            task_id = event.task_id
                        elif hasattr(event, 'taskId'):
                            task_id = event.taskId
                        
                        if hasattr(event, 'artifact') and hasattr(event.artifact, 'parts'):
                            texts = extract_text_from_parts(event.artifact.parts)
                            
                            if texts:
                                if not agent_responded:
                                    progress.stop()
                                    console.print("[bold green]Agent:[/bold green]")
                                    agent_responded = True
                                
                                # Show artifact with emphasis
                                for text in texts:
                                    console.print(f"[bold]{text}[/bold]")
                                final_artifact_shown = True
                    
                    elif isinstance(event, Message):
                        response_message = event
                        if not agent_responded:
                            progress.stop()
                            console.print("[bold green]Agent:[/bold green]")
                            agent_responded = True
                        
                        texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
                        for text in texts:
                            console.print(text)
                
                # Ensure progress is stopped
                if not agent_responded:
                    progress.stop()
            
            except Exception as e:
                progress.stop()
                console.print(f"[bold red]✗ Stream error:[/bold red] {e}")
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return False, context_id, task_id
        
        # Add newline after response if we got one
        if agent_responded and not final_artifact_shown:
            console.print()
        elif agent_responded and final_artifact_shown:
            console.print()
        
        # If no response was shown, try to fetch the task result
        if task_id and not agent_responded:
            if debug:
                console.print("[dim]Fetching task result...[/dim]")
            try:
                taskResultResponse = await client.get_task(
                    GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task_id))
                )
                if isinstance(taskResultResponse.root, JSONRPCErrorResponse):
                    console.print(f"[bold red]✗ Error:[/bold red] {taskResultResponse.root.error}")
                    return False, context_id, task_id
                
                taskResult = taskResultResponse.root.result
                
                # Try to extract message from task result
                if hasattr(taskResult, 'status') and hasattr(taskResult.status, 'message'):
                    msg = taskResult.status.message
                    console.print("[bold green]Agent:[/bold green]")
                    texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                    for text in texts:
                        console.print(text)
                    console.print()
            except Exception as e:
                if debug:
                    console.print(f"[dim]Could not fetch task: {e}[/dim]")
    
    else:
        # Non-streaming mode
        with console.status("[bold green]Thinking...", spinner="dots"):
            try:
                event = await client.send_message(
                    SendMessageRequest(id=str(uuid4()), params=payload)
                )
                event = event.root.result
            except Exception as e:
                console.print(f"[bold red]✗ Request failed:[/bold red] {e}")
                return False, context_id, task_id
        
        if hasattr(event, 'context_id'):
            context_id = event.context_id
        elif hasattr(event, 'contextId'):
            context_id = event.contextId
        
        if isinstance(event, Task):
            if not task_id:
                task_id = event.id
            taskResult = event
        elif isinstance(event, Message):
            response_message = event
            console.print("\n[bold green]Agent:[/bold green]")
            texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
            for text in texts:
                console.print(text)
            console.print()

    if taskResult:
        state = TaskState(taskResult.status.state)
        
        if state.name == TaskState.input_required.name:
            if debug:
                console.print("[dim]⚠ Agent requires more input (continuing conversation)[/dim]")
            return await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                task_id,
                context_id,
                debug,
            )
        
        return True, context_id, task_id
    
    return True, context_id, task_id


if __name__ == '__main__':
    asyncio.run(cli())
