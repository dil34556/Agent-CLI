import asyncio
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
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box
from rich.text import Text
from rich.align import Align
from dotenv import load_dotenv, set_key
from a2a.client import A2ACardResolver, A2AClient
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.types import (
    GetTaskRequest,
    JSONRPCErrorResponse,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    SendMessageRequest,
    SendStreamingMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskQueryParams,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
import json
import sys

# Suppress deprecation warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

console = Console()

# Define config file paths
CONFIG_DIR = Path(__file__).parent / '.a2a_config'
CONFIG_FILE = CONFIG_DIR / 'agents.json'
ENV_FILE = Path(__file__).parent / '.env'

# Load existing .env if it exists
load_dotenv(ENV_FILE)


def load_agents_config():
    """Load saved agents configuration"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_agents_config(config):
    """Save agents configuration"""
    CONFIG_DIR.mkdir(exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def display_api_error(error_response):
    """Display API error in a user-friendly format"""
    if isinstance(error_response, dict):
        if "error" in error_response:
            error = error_response["error"]
            
            # Build error panel content
            error_content = f"[bold red]{error.get('message', 'An error occurred')}[/bold red]\n\n"
            
            if "details" in error:
                error_content += f"[yellow]{error['details']}[/yellow]\n"
            
            # Display error code if available
            if "code" in error:
                error_content += f"\n[dim]Error Code: {error['code']}[/dim]"
            
            console.print(Panel(
                error_content,
                title="[bold red]⚠ Error[/bold red]",
                border_style="red",
                expand=False
            ))
            return True
    return False


async def handle_http_error(e: httpx.HTTPStatusError, context: str = "request") -> bool:
    """
    Centralized HTTP error handler
    Returns True if error was displayed and should stop execution
    """
    try:
        error_data = e.response.json()
        if "error" in error_data or "success" in error_data:
            # It's our custom error format
            display_api_error(error_data)
            return True
    except Exception:
        pass
    
    # Fallback error messages
    if e.response.status_code == 401:
        console.print(Panel(
            "[bold red]Authentication Failed[/bold red]\n\n"
            "[yellow]Your credentials are invalid.[/yellow]\n"
            "Please check and try again.",
            title="[bold red]⚠ Unauthorized[/bold red]",
            border_style="red"
        ))
    elif e.response.status_code == 429:
        console.print(Panel(
            "[bold red]Rate Limit Exceeded[/bold red]\n\n"
            "[yellow]Too many requests. Please wait.[/yellow]",
            title="[bold red]⚠ Too Many Requests[/bold red]",
            border_style="red"
        ))
    elif e.response.status_code == 503:
        console.print(Panel(
            "[bold red]Service Unavailable[/bold red]\n\n"
            "[yellow]The service is down. Try again later.[/yellow]",
            title="[bold red]⚠ Service Error[/bold red]",
            border_style="red"
        ))
    else:
        console.print(f"[bold red]✗ HTTP Error {e.response.status_code}:[/bold red] {e}")
    
    return True


async def fetch_agent_card(agent_url: str, headers: dict = None):
    """Fetch agent card to determine authentication requirements"""
    async with httpx.AsyncClient(timeout=30, headers=headers or {}) as client:
        try:
            card_resolver = A2ACardResolver(client, agent_url, agent_card_path="/.well-known/agent.json")
            card = await card_resolver.get_agent_card()
            return card
        except Exception:
            return None


def get_security_schemes_from_card(card):
    """Extract security schemes from agent card"""
    if not card:
        return None
    
    # Try multiple possible attribute names
    security_schemes = None
    
    if hasattr(card, 'securitySchemes') and card.securitySchemes:
        security_schemes = card.securitySchemes
    elif hasattr(card, 'security_schemes') and card.security_schemes:
        security_schemes = card.security_schemes
    
    # Also check if it's in a dict format
    if not security_schemes and isinstance(card, dict):
        security_schemes = card.get('securitySchemes') or card.get('security_schemes')
    
    return security_schemes

async def setup_agent_auth(agent_url: str):
    """Setup authentication for a specific agent based on its card"""
    
    console.print(f"\n[bold cyan]Setting up: {agent_url}[/bold cyan]")
    
    # Fetch agent card to detect security requirements
    with console.status("[dim]Checking agent...", spinner="dots"):
        try:
            card = await fetch_agent_card(agent_url)
        except Exception as e:
            console.print(f"[red]✗ Failed to fetch agent card: {e}[/red]")
            card = None
    
    if not card:
        console.print("[yellow]⚠ Cannot fetch agent card. Assuming no authentication.[/yellow]")
        return {
            'url': agent_url,
            'name': 'Agent',
            'auth_type': 'none'
        }
    
    agent_config = {
        'url': agent_url,
        'name': card.name if hasattr(card, 'name') and card.name else 'Agent',
        'auth_type': 'none'
    }
    
    # Get security schemes - try multiple ways
    security_schemes = get_security_schemes_from_card(card)
    
    # Debug: Check if card object has any security-related attributes
    if not security_schemes:
        # Try to access raw dict if card is a model
        if hasattr(card, 'model_dump'):
            card_dict = card.model_dump()
            security_schemes = card_dict.get('securitySchemes') or card_dict.get('security_schemes')
        elif hasattr(card, 'dict'):
            card_dict = card.dict()
            security_schemes = card_dict.get('securitySchemes') or card_dict.get('security_schemes')
    
    if not security_schemes:
        console.print("[green]✓ No authentication required[/green]")
        return agent_config
    
    # Process security schemes dynamically
    console.print("[yellow]⚠ Authentication required[/yellow]\n")
    
    # Convert to dict if it's an object
    if not isinstance(security_schemes, dict):
        if hasattr(security_schemes, 'model_dump'):
            security_schemes = security_schemes.model_dump()
        elif hasattr(security_schemes, 'dict'):
            security_schemes = security_schemes.dict()
        elif hasattr(security_schemes, '__dict__'):
            security_schemes = security_schemes.__dict__
    
    # Handle different security scheme types
    for scheme_name, scheme_info in security_schemes.items():
        # Convert scheme_info to dict if needed
        if not isinstance(scheme_info, dict):
            if hasattr(scheme_info, 'model_dump'):
                scheme_info = scheme_info.model_dump()
            elif hasattr(scheme_info, 'dict'):
                scheme_info = scheme_info.dict()
            elif hasattr(scheme_info, '__dict__'):
                scheme_info = scheme_info.__dict__
        
        scheme_type = scheme_info.get('type', '')
        
        if scheme_type == 'apiKey':
            # Get header name - try different field names
            header_name = (
                scheme_info.get('name') or 
                scheme_info.get('in_') or 
                scheme_name
            )
            
            description = scheme_info.get('description', '')
            
            # Show description if available
            if description:
                console.print(f"[dim]ℹ  {description}[/dim]\n")
            
            # Use the exact header name in the prompt
            api_key = Prompt.ask(
                f"[bold cyan]Enter your {header_name}[/bold cyan]",
                password=True
            )
            
            if not api_key or not api_key.strip():
                console.print("[red]✗ API key is required[/red]")
                return None
            
            agent_config['auth_type'] = 'api-key'
            agent_config['api_key_header'] = header_name
            agent_config['api_key'] = api_key
            console.print("[green]✓ Authentication configured[/green]")
            break
            
        elif scheme_type == 'bearer' or scheme_type == 'http':
            description = scheme_info.get('description', 'Bearer token authentication required')
            
            if description:
                console.print(f"[dim]ℹ  {description}[/dim]\n")
            
            bearer_token = Prompt.ask(
                "[bold cyan]Enter your Bearer Token[/bold cyan]",
                password=True
            )
            
            if not bearer_token or not bearer_token.strip():
                console.print("[red]✗ Bearer token is required[/red]")
                return None
            
            agent_config['auth_type'] = 'bearer'
            agent_config['bearer_token'] = bearer_token
            console.print("[green]✓ Authentication configured[/green]")
            break
    
    return agent_config


def create_menu_item(icon: str, title: str, description: str, is_selected: bool = False) -> Panel:
    """Create a styled menu item"""
    if is_selected:
        content = Text()
        content.append(f"{icon} ", style="bold yellow")
        content.append(title, style="bold yellow")
        content.append(f"\n{description}", style="dim yellow")
        
        return Panel(
            content,
            border_style="bold yellow",
            box=box.HEAVY,
            padding=(0, 1),
            expand=False
        )
    else:
        content = Text()
        content.append(f"{icon} ", style="cyan")
        content.append(title, style="white")
        content.append(f"\n{description}", style="dim")
        
        return Panel(
            content,
            border_style="dim",
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False
        )


def select_agent_interactive(agents_config):
    """Interactive agent selection with arrow keys"""
    if not agents_config:
        return None
    
    # Try to import keyboard support
    try:
        import readchar
        has_readchar = True
    except ImportError:
        has_readchar = False
        console.print("[yellow]⚠ Install 'readchar' for arrow key support: pip install readchar[/yellow]\n")
    
    # Build options
    options = []
    agent_list = list(agents_config.items())
    
    for agent_id, config in agent_list:
        name = config.get('name', 'Unknown')
        url = config['url']
        auth_type = config.get('auth_type', 'none')
        display_url = url[:50] + '...' if len(url) > 50 else url
        auth_icon = "🔒" if auth_type != 'none' else "🔓"
        
        options.append({
            'icon': '◉',
            'title': name,
            'description': f"{auth_icon} {display_url}",
            'value': ('chat', agent_id, config)
        })
    
    # Add special options
    options.append({
        'icon': '➕',
        'title': 'Add New Agent',
        'description': 'Configure a new agent',
        'value': ('add', None, None)
    })
    
    options.append({
        'icon': '❌',
        'title': 'Exit',
        'description': 'Quit the application',
        'value': ('exit', None, None)
    })
    
    selected_index = 0
    
    if has_readchar:
        # Interactive mode with arrow keys
        console.print("\n[bold cyan]💬 Your Agents[/bold cyan]")
        console.print("[dim]Use ↑/↓ arrow keys to navigate, Enter to select[/dim]\n")
        
        while True:
            # Clear and redraw menu
            console.clear()
            console.print("\n[bold cyan]💬 Your Agents[/bold cyan]")
            console.print("[dim]Use ↑/↓ arrow keys to navigate, Enter to select[/dim]\n")
            
            # Display all options
            for idx, option in enumerate(options):
                is_selected = (idx == selected_index)
                panel = create_menu_item(
                    option['icon'],
                    option['title'],
                    option['description'],
                    is_selected
                )
                console.print(panel)
            
            # Get key input
            key = readchar.readkey()
            
            if key == readchar.key.UP:
                selected_index = (selected_index - 1) % len(options)
            elif key == readchar.key.DOWN:
                selected_index = (selected_index + 1) % len(options)
            elif key == readchar.key.ENTER or key == '\r' or key == '\n':
                break
            elif key == 'q' or key == 'Q':
                return ('exit', None, None)
    else:
        # Fallback: numbered selection
        console.print("\n[bold cyan]💬 Your Agents[/bold cyan]\n")
        
        for idx, option in enumerate(options):
            console.print(f"[cyan]{idx + 1}.[/cyan] {option['icon']} [bold]{option['title']}[/bold]")
            console.print(f"   [dim]{option['description']}[/dim]\n")
        
        while True:
            choice = Prompt.ask(
                "[bold cyan]Select an option[/bold cyan]",
                default="1"
            )
            
            try:
                selected_index = int(choice) - 1
                if 0 <= selected_index < len(options):
                    break
                else:
                    console.print(f"[red]Please enter a number between 1 and {len(options)}[/red]")
            except ValueError:
                console.print("[red]Please enter a valid number[/red]")
    
    return options[selected_index]['value']


def build_headers_for_agent(agent_config, additional_headers=None):
    """Build HTTP headers based on agent configuration"""
    headers = additional_headers.copy() if additional_headers else {}
    
    auth_type = agent_config.get('auth_type', 'none')
    
    if auth_type == 'api-key' and 'api_key' in agent_config:
        # Use dynamic header name if available
        header_name = agent_config.get('api_key_header', 'X-API-Key')
        headers[header_name] = agent_config['api_key']
    elif auth_type == 'bearer' and 'bearer_token' in agent_config:
        headers['Authorization'] = f"Bearer {agent_config['bearer_token']}"
    elif auth_type == 'custom' and 'custom_header' in agent_config:
        custom = agent_config['custom_header']
        headers[custom['name']] = custom['value']
    
    return headers


@click.command()
@click.argument('agent_url', required=False, metavar='URL')
@click.option('--agent', 'agent_option', help='Agent URL (e.g. http://127.0.0.1:10007/)')
@click.option('--add', is_flag=True, help='Add new agent')
@click.option('--list', 'list_agents', is_flag=True, help='List agents')
@click.option('--remove', help='Remove agent ID')
@click.option('--bearer-token', envvar='A2A_CLI_BEARER_TOKEN')
@click.option('--api-key', envvar='Telmini-API-Key')
@click.option('--session', default=0, help='Session ID')
@click.option('--history', is_flag=True, help='Show history')
@click.option('--use_push_notifications', is_flag=True)
@click.option('--push_notification_receiver', default='http://localhost:5000')
@click.option('--header', multiple=True, help='Header: key=value')
@click.option('--enabled_extensions', default='')
@click.option('--debug', is_flag=True)
@click.option('--reset', is_flag=True, help='Reset all config')
async def cli(
    agent_url,
    agent_option,
    add,
    list_agents,
    remove,
    bearer_token,
    api_key,
    session,
    history,
    use_push_notifications: bool,
    push_notification_receiver: str,
    header,
    enabled_extensions,
    debug,
    reset,
):
    """A2A Multi-Agent CLI.

    Examples:
      uv run . --add --agent http://127.0.0.1:10007/
      uv run . http://127.0.0.1:10007/
      uv run .
    """
    
    agent = agent_option or agent_url
    
    # Print banner
    console.print(Panel.fit(
        "[bold cyan]A2A Multi-Agent CLI[/bold cyan]\n"
        "[dim]Talk to multiple agents[/dim]\n\n"
        "[dim]Examples:\n"
        "  uv run . --add --agent http://127.0.0.1:10007/\n"
        "  uv run . http://127.0.0.1:10007/\n"
        "  uv run .[/dim]",
        border_style="cyan"
    ))
    
    # Reset config
    if reset:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
            console.print("[green]✓ Config reset[/green]")
        if ENV_FILE.exists():
            ENV_FILE.unlink()
            console.print("[green]✓ .env reset[/green]")
        return
    
    # Load agents
    agents_config = load_agents_config()
    
    # List agents
    if list_agents:
        if not agents_config:
            console.print("[yellow]No agents yet. Use --add[/yellow]")
        else:
            console.print("\n[bold]Saved Agents[/bold]\n")
            for idx, (agent_id, config) in enumerate(agents_config.items(), 1):
                name = config.get('name', 'Unknown')
                url = config['url']
                auth_type = config.get('auth_type', 'unknown')
                
                console.print(f"[cyan]{idx}.[/cyan] [green]{name}[/green]")
                console.print(f"   [dim]ID: {agent_id} • URL: {url} • Auth: {auth_type}[/dim]")
        return
    
    # Remove agent
    if remove:
        if remove in agents_config:
            removed_name = agents_config[remove].get('name', remove)
            del agents_config[remove]
            save_agents_config(agents_config)
            console.print(f"[green]✓ Removed '{removed_name}'[/green]")
        else:
            console.print(f"[red]✗ ID '{remove}' not found[/red]")
        return
    
    # Add new agent
    if add:
        if not agent:
            agent = Prompt.ask("[bold cyan]Agent URL[/bold cyan]")
        
        agent_config = await setup_agent_auth(agent)
        agent_id = str(uuid4())[:8]
        agents_config[agent_id] = agent_config
        save_agents_config(agents_config)
        
        console.print(f"\n[green]✓ Saved as ID: [bold]{agent_id}[/bold][/green]")
        
        if not Confirm.ask("\n[cyan]Chat now?[/cyan]", default=True):
            return
        
        selected_agent_id = agent_id
        selected_agent_config = agent_config
    
    # Select from existing agents with interactive UI
    elif not agent and agents_config:
        result = select_agent_interactive(agents_config)
        if not result:
            console.print("[yellow]No agent selected[/yellow]")
            return
        
        action, agent_id, agent_config = result
        
        if action == 'exit':
            console.print("\n[yellow]👋 Goodbye![/yellow]")
            return
        elif action == 'add':
            # Trigger add agent flow
            agent = Prompt.ask("[bold cyan]Agent URL[/bold cyan]")
            agent_config = await setup_agent_auth(agent)
            new_agent_id = str(uuid4())[:8]
            agents_config[new_agent_id] = agent_config
            save_agents_config(agents_config)
            console.print(f"\n[green]✓ Saved as ID: [bold]{new_agent_id}[/bold][/green]")
            
            if not Confirm.ask("\n[cyan]Chat now?[/cyan]", default=True):
                return
            
            selected_agent_id = new_agent_id
            selected_agent_config = agent_config
        elif action == 'chat':
            selected_agent_id = agent_id
            selected_agent_config = agent_config
        else:
            console.print("[yellow]Invalid selection[/yellow]")
            return
    
    # First time setup or direct URL
    elif agent:
        # Direct URL provided - set it up dynamically
        agent_config = await setup_agent_auth(agent)
        
        # Ask if user wants to save this agent
        if Confirm.ask("\n[cyan]Save this agent for future use?[/cyan]", default=True):
            agent_id = str(uuid4())[:8]
            agents_config[agent_id] = agent_config
            save_agents_config(agents_config)
            console.print(f"[green]✓ Saved (ID: {agent_id})[/green]")
            selected_agent_id = agent_id
        else:
            selected_agent_id = 'temp'
        
        selected_agent_config = agent_config
    
    else:
        # No agents saved, no URL provided
        console.print(
            "\n[bold yellow]First Time Setup[/bold yellow]\n"
            "[dim]No agents saved yet. Add one with `uv run . --add --agent URL` "
            "or enter a URL now.[/dim]\n"
        )
        agent = Prompt.ask("[cyan]Agent URL[/cyan]")
        
        agent_config = await setup_agent_auth(agent)
        agent_id = str(uuid4())[:8]
        agents_config[agent_id] = agent_config
        save_agents_config(agents_config)
        
        console.print(f"\n[green]✓ Saved (ID: {agent_id})[/green]")
        selected_agent_id = agent_id
        selected_agent_config = agent_config
    
    # Build headers
    additional_headers = {}
    for h in header:
        if '=' in h:
            k, v = h.split('=', 1)
            additional_headers[k] = v
    
    if enabled_extensions:
        ext_list = [ext.strip() for ext in enabled_extensions.split(',') if ext.strip()]
        if ext_list:
            additional_headers[HTTP_EXTENSION_HEADER] = ', '.join(ext_list)
    
    headers = build_headers_for_agent(selected_agent_config, additional_headers)
    
    agent_url = selected_agent_config['url']
    
    async with httpx.AsyncClient(timeout=30, headers=headers) as httpx_client:
        # Connect to agent
        with console.status("[green]Connecting...", spinner="dots"):
            try:
                card_resolver = A2ACardResolver(httpx_client, agent_url, agent_card_path="/.well-known/agent.json")
                card = await card_resolver.get_agent_card()
            except httpx.HTTPStatusError as e:
                if await handle_http_error(e, "connection"):
                    return
            except Exception as e:
                console.print(f"\n[red]✗ Failed to connect:[/red] {e}")
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return

        console.print("[green]✓ Connected[/green]\n")
        
        # Display agent info
        info = Table(show_header=False, box=box.ROUNDED, border_style="green", title="[bold]Agent Info[/bold]")
        info.add_column("", style="cyan", width=15)
        info.add_column("", style="white")
        
        info.add_row("Name", card.name or "Unknown")
        if card.description:
            info.add_row("About", card.description[:60] + '...' if len(card.description) > 60 else card.description)
        info.add_row("Version", card.version or "N/A")
        info.add_row("Streaming", "✓" if card.capabilities.streaming else "✗")
  
        
        console.print(info)

        if debug and card.skills:
            console.print("\n[bold]Skills:[/bold]")
            for skill in card.skills[:5]:  # Show first 5 only
                console.print(f"  [cyan]•[/cyan] {skill.name}")

        if debug and headers:
            console.print("\n[bold]Headers:[/bold]")
            for key, value in headers.items():
                display_value = value if 'token' not in key.lower() and 'key' not in key.lower() else "***"
                console.print(f"  [cyan]{key}:[/cyan] {display_value}")

        notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
        notification_receiver_host = notif_receiver_parsed.hostname
        notification_receiver_port = notif_receiver_parsed.port

        if use_push_notifications:
            from hosts.cli.push_notification_listener import PushNotificationListener
            
            with console.status("[yellow]Starting push listener..."):
                push_notification_listener = PushNotificationListener(
                    host=notification_receiver_host,
                    port=notification_receiver_port,
                )
                push_notification_listener.start()
            console.print("[green]✓ Push enabled[/green]")

        client = A2AClient(httpx_client, agent_card=card, url=agent_url.rstrip('/'))
        continue_loop = True
        streaming = card.capabilities.streaming
        context_id = session if session > 0 else uuid4().hex
        
        console.print(f"\n[dim]Session: {context_id[:16]}...[/dim]")
        console.print("[dim]Type 'exit' or 'quit' to leave, 'switch' to change agent[/dim]")
        console.print()

        while continue_loop:
            continue_loop, _, task_id, command = await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                None,
                context_id,
                debug,
            )
            
            # Handle switch command
            if command == 'switch':
                if len(agents_config) > 1:
                    console.print("\n[yellow]Restart to switch:[/yellow]")
                    console.print(f"[dim]uv run . (then choose agent)[/dim]")
                else:
                    console.print("[yellow]Only one agent saved. Add more with --add[/yellow]")
                continue_loop = False

            if history and continue_loop and task_id:
                console.print("\n[cyan]━━━ History ━━━[/cyan]")
                try:
                    task_response = await client.get_task({'id': task_id, 'historyLength': 10})
                    
                    if hasattr(task_response.root, 'result') and hasattr(task_response.root.result, 'history'):
                        for msg in task_response.root.result.history:
                            role_color = "blue" if msg.role == "user" else "green"
                            role_label = "You" if msg.role == "user" else "Agent"
                            console.print(f"\n[bold {role_color}]{role_label}:[/bold {role_color}]")
                            for part in msg.parts:
                                if hasattr(part, 'text'):
                                    console.print(f"  {part.text}")
                except httpx.HTTPStatusError as e:
                    await handle_http_error(e, "history")
                except Exception as e:
                    if debug:
                        console.print(f"[dim]History error: {e}[/dim]")


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
    
    # Exit commands
    if not prompt or prompt.lower() in ['quit', 'exit', 'q', 'ext', 'qt']:
        console.print("\n[yellow]👋 Goodbye![/yellow]")
        return False, None, None, None
    
    # Switch command
    if prompt.lower() in ['switch', 'agents']:
        return False, None, None, 'switch'
    
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
    agent_responded = False
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
                        console.print(f"[dim]Event: {result.root}[/dim]")
                    
                    if isinstance(result.root, JSONRPCErrorResponse):
                        progress.stop()
                        console.print(f"[red]✗ Error: {result.root.error}[/red]")
                        return False, context_id, task_id, None
                    
                    event = result.root.result
                    
                    # Extract context_id
                    if hasattr(event, 'context_id'):
                        context_id = event.context_id
                    elif hasattr(event, 'contextId'):
                        context_id = event.contextId
                    
                    if isinstance(event, Task):
                        task_id = event.id
                        if debug:
                            progress.update(progress_task, description=f"[cyan]Task: {task_id[:8]}...[/cyan]")
                    
                    elif isinstance(event, TaskStatusUpdateEvent):
                        if hasattr(event, 'task_id'):
                            task_id = event.task_id
                        elif hasattr(event, 'taskId'):
                            task_id = event.taskId
                        
                        status_state = event.status.state if hasattr(event.status, 'state') else 'unknown'
                        
                        if debug:
                            progress.update(progress_task, description=f"[cyan]{status_state}[/cyan]")
                        
                        # Working state messages
                        if status_state == 'working' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts and not agent_responded:
                                progress.stop()
                                console.print("[bold yellow]Agent:[/bold yellow]")
                                agent_responded = True
                            
                            for text in texts:
                                console.print(f"[dim]{text}[/dim]")
                        
                        # Input-required messages
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
                        
                        # Completed
                        if status_state == 'completed':
                            if not agent_responded:
                                progress.stop()
                    
                    elif isinstance(event, TaskArtifactUpdateEvent):
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
                                
                                for text in texts:
                                    console.print(f"[bold]{text}[/bold]")
                                final_artifact_shown = True
                    
                    elif isinstance(event, Message):
                        if not agent_responded:
                            progress.stop()
                            console.print("[bold green]Agent:[/bold green]")
                            agent_responded = True
                        
                        texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
                        for text in texts:
                            console.print(text)
                
                if not agent_responded:
                    progress.stop()
            
            except httpx.HTTPStatusError as e:
                progress.stop()
                
                if e.response.status_code == 400:
                    try:
                        error_data = e.response.json()
                        if display_api_error(error_data):
                            return False, context_id, task_id, None
                    except Exception:
                        pass
                
                await handle_http_error(e, "streaming")
                return False, context_id, task_id, None
            
            except Exception as e:
                progress.stop()
                
                error_msg = str(e)
                if "text/event-stream" in error_msg and "application/json" in error_msg:
                    console.print(Panel(
                        "[bold red]Auth Error[/bold red]\n\n"
                        "[yellow]Invalid credentials or service issue.[/yellow]\n"
                        "Check your API key.",
                        title="[bold red]⚠ Stream Error[/bold red]",
                        border_style="red"
                    ))
                else:
                    console.print(f"[red]✗ Stream error: {e}[/red]")
                
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return False, context_id, task_id, None
        
        if agent_responded:
            console.print()
        
        # Fetch task if no response
        if task_id and not agent_responded:
            if debug:
                console.print("[dim]Fetching task...[/dim]")
            try:
                taskResultResponse = await client.get_task(
                    GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task_id))
                )
                if isinstance(taskResultResponse.root, JSONRPCErrorResponse):
                    console.print(f"[red]✗ Error: {taskResultResponse.root.error}[/red]")
                    return False, context_id, task_id, None
                
                taskResult = taskResultResponse.root.result
                
                if hasattr(taskResult, 'status') and hasattr(taskResult.status, 'message'):
                    msg = taskResult.status.message
                    console.print("[bold green]Agent:[/bold green]")
                    texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                    for text in texts:
                        console.print(text)
                    console.print()
            except httpx.HTTPStatusError as e:
                await handle_http_error(e, "task fetch")
            except Exception as e:
                if debug:
                    console.print(f"[dim]Task fetch error: {e}[/dim]")
    
    else:
        # Non-streaming mode
        with console.status("[green]Thinking...", spinner="dots"):
            try:
                event = await client.send_message(
                    SendMessageRequest(id=str(uuid4()), params=payload)
                )
                event = event.root.result
            except httpx.HTTPStatusError as e:
                await handle_http_error(e, "message send")
                return False, context_id, task_id, None
            except Exception as e:
                console.print(f"[red]✗ Request failed: {e}[/red]")
                return False, context_id, task_id, None
        
        if hasattr(event, 'context_id'):
            context_id = event.context_id
        elif hasattr(event, 'contextId'):
            context_id = event.contextId
        
        if isinstance(event, Task):
            if not task_id:
                task_id = event.id
            taskResult = event
        elif isinstance(event, Message):
            console.print("\n[bold green]Agent:[/bold green]")
            texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
            for text in texts:
                console.print(text)
            console.print()

    if taskResult:
        state = TaskState(taskResult.status.state)
        
        if state.name == TaskState.input_required.name:
            if debug:
                console.print("[dim]Agent needs more input[/dim]")
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
        
        return True, context_id, task_id, None
    
    return True, context_id, task_id, None


if __name__ == '__main__':
    asyncio.run(cli())