import asyncio
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
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich import box
from rich.text import Text
from dotenv import load_dotenv
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
import time
from datetime import datetime
from src import banner_lines
# Suppress deprecation warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

console = Console()

# Define config file paths
CONFIG_DIR = Path(__file__).parent / '.a2a_config'
CONFIG_FILE = CONFIG_DIR / 'agents.json'
ENV_FILE = Path(__file__).parent / '.env'

# Load existing .env if it exists
load_dotenv(ENV_FILE)


def animated_banner():
    """Display animated Telminator banner"""
    # Animate banner appearance
    for i, line in enumerate(banner_lines):
        if i == 0 or i == len(banner_lines) - 1:
            console.print(line, style="bold cyan")
        elif i == 2 or i == 7:
            console.print(line, style="bold magenta")
        elif i == 9:
            console.print(line, style="bold yellow")
        else:
            console.print(line, style="cyan")
        time.sleep(0.05)
    
    console.print()


def pulse_text(text: str, style: str = "bold cyan"):
    """Create a pulsing text effect"""
    styles = [f"dim {style}", style, f"bold {style}"]
    for s in styles:
        console.print(f"\r{text}", style=s, end="")
        time.sleep(0.2)
    console.print()


def typewriter_effect(text: str, style: str = "white", delay: float = 0.03):
    """Typewriter animation for text"""
    for char in text:
        console.print(char, style=style, end="")
        time.sleep(delay)
    console.print()


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
                title="[bold red]⚠️  ERROR  ⚠️[/bold red]",
                border_style="red",
                box=box.DOUBLE,
                expand=False
            ))
            return True
    return False


async def handle_http_error(e: httpx.HTTPStatusError, context: str = "request") -> bool:
    """Centralized HTTP error handler"""
    try:
        error_data = e.response.json()
        if "error" in error_data or "success" in error_data:
            display_api_error(error_data)
            return True
    except Exception:
        pass
    
    # Fallback error messages with better styling
    if e.response.status_code == 401:
        console.print(Panel(
            "[bold red]🔒 Authentication Failed[/bold red]\n\n"
            "[yellow]Your credentials are invalid or expired.[/yellow]\n\n"
            "[dim]→ Check your API key or bearer token\n"
            "→ Verify the agent URL is correct[/dim]",
            title="[bold red]⚠️  UNAUTHORIZED  ⚠️[/bold red]",
            border_style="red",
            box=box.DOUBLE
        ))
    elif e.response.status_code == 429:
        console.print(Panel(
            "[bold red]⏱️  Rate Limit Exceeded[/bold red]\n\n"
            "[yellow]Too many requests. Please wait a moment.[/yellow]",
            title="[bold red]⚠️  RATE LIMITED  ⚠️[/bold red]",
            border_style="red",
            box=box.DOUBLE
        ))
    elif e.response.status_code == 503:
        console.print(Panel(
            "[bold red]🔧 Service Unavailable[/bold red]\n\n"
            "[yellow]The service is temporarily down.[/yellow]\n"
            "[dim]Please try again in a few minutes.[/dim]",
            title="[bold red]⚠️  SERVICE ERROR  ⚠️[/bold red]",
            border_style="red",
            box=box.DOUBLE
        ))
    else:
        console.print(Panel(
            f"[bold red]HTTP {e.response.status_code}[/bold red]\n\n"
            f"[yellow]{str(e)}[/yellow]",
            title="[bold red]⚠️  ERROR  ⚠️[/bold red]",
            border_style="red",
            box=box.DOUBLE
        ))
    
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
    
    security_schemes = None
    
    if hasattr(card, 'securitySchemes') and card.securitySchemes:
        security_schemes = card.securitySchemes
    elif hasattr(card, 'security_schemes') and card.security_schemes:
        security_schemes = card.security_schemes
    
    if not security_schemes and isinstance(card, dict):
        security_schemes = card.get('securitySchemes') or card.get('security_schemes')
    
    return security_schemes


async def setup_agent_auth(agent_url: str):
    """Setup authentication for a specific agent based on its card"""
    
    console.print()
    console.print(Panel(
        f"[bold cyan]🔗 Connecting to Agent[/bold cyan]\n\n"
        f"[dim]{agent_url}[/dim]",
        border_style="cyan",
        box=box.ROUNDED
    ))
    
    # Animated loading
    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[cyan]{task.description}"),
        console=console,
        transient=True
    ) as progress:
        task = progress.add_task("Fetching agent information...", total=None)
        
        try:
            card = await fetch_agent_card(agent_url)
        except Exception as e:
            console.print(f"[red]✗ Failed to fetch agent card: {e}[/red]")
            card = None
    
    if not card:
        console.print(Panel(
            "[yellow]⚠️  Cannot fetch agent card[/yellow]\n\n"
            "[dim]Proceeding without authentication...[/dim]",
            border_style="yellow",
            box=box.ROUNDED
        ))
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
    
    security_schemes = get_security_schemes_from_card(card)
    
    if not security_schemes:
        console.print(Panel(
            "[green]✓ No authentication required[/green]\n\n"
            "[dim]This agent is publicly accessible[/dim]",
            border_style="green",
            box=box.ROUNDED
        ))
        return agent_config
    
    # Authentication required
    console.print(Panel(
        "[yellow]🔐 Authentication Required[/yellow]\n\n"
        "[dim]This agent requires credentials to access[/dim]",
        border_style="yellow",
        box=box.ROUNDED
    ))
    
    # Convert to dict if needed
    if not isinstance(security_schemes, dict):
        if hasattr(security_schemes, 'model_dump'):
            security_schemes = security_schemes.model_dump()
        elif hasattr(security_schemes, 'dict'):
            security_schemes = security_schemes.dict()
        elif hasattr(security_schemes, '__dict__'):
            security_schemes = security_schemes.__dict__
    
    # Handle different security scheme types
    for scheme_name, scheme_info in security_schemes.items():
        if not isinstance(scheme_info, dict):
            if hasattr(scheme_info, 'model_dump'):
                scheme_info = scheme_info.model_dump()
            elif hasattr(scheme_info, 'dict'):
                scheme_info = scheme_info.dict()
            elif hasattr(scheme_info, '__dict__'):
                scheme_info = scheme_info.__dict__
        
        scheme_type = scheme_info.get('type', '')
        
        if scheme_type == 'apiKey':
            header_name = (
                scheme_info.get('name') or 
                scheme_info.get('in_') or 
                scheme_name
            )
            
            description = scheme_info.get('description', '')
            
            if description:
                console.print(f"\n[dim]ℹ️  {description}[/dim]")
            
            api_key = Prompt.ask(
                f"\n[bold cyan]🔑 Enter your {header_name}[/bold cyan]",
                password=True
            )
            
            if not api_key or not api_key.strip():
                console.print("[red]✗ API key is required[/red]")
                return None
            
            agent_config['auth_type'] = 'api-key'
            agent_config['api_key_header'] = header_name
            agent_config['api_key'] = api_key
            
            console.print(Panel(
                "[green]✓ Authentication configured successfully[/green]",
                border_style="green",
                box=box.ROUNDED
            ))
            break
            
        elif scheme_type == 'bearer' or scheme_type == 'http':
            description = scheme_info.get('description', 'Bearer token authentication required')
            
            if description:
                console.print(f"\n[dim]ℹ️  {description}[/dim]")
            
            bearer_token = Prompt.ask(
                "\n[bold cyan]🎫 Enter your Bearer Token[/bold cyan]",
                password=True
            )
            
            if not bearer_token or not bearer_token.strip():
                console.print("[red]✗ Bearer token is required[/red]")
                return None
            
            agent_config['auth_type'] = 'bearer'
            agent_config['bearer_token'] = bearer_token
            
            console.print(Panel(
                "[green]✓ Authentication configured successfully[/green]",
                border_style="green",
                box=box.ROUNDED
            ))
            break
    
    return agent_config


def create_menu_item(icon: str, title: str, description: str, is_selected: bool = False) -> Panel:
    """Create a styled menu item"""
    if is_selected:
        content = Text()
        content.append(f"{icon}  ", style="bold yellow")
        content.append(title, style="bold yellow")
        content.append(f"\n   {description}", style="yellow")
        
        return Panel(
            content,
            border_style="bold yellow",
            box=box.DOUBLE,
            padding=(0, 1),
            expand=False
        )
    else:
        content = Text()
        content.append(f"{icon}  ", style="cyan")
        content.append(title, style="white")
        content.append(f"\n   {description}", style="dim")
        
        return Panel(
            content,
            border_style="dim cyan",
            box=box.ROUNDED,
            padding=(0, 1),
            expand=False
        )


def select_agent_interactive(agents_config):
    """Interactive agent selection with arrow keys"""
    if not agents_config:
        return None
    
    try:
        import readchar
        has_readchar = True
    except ImportError:
        has_readchar = False
        console.print(Panel(
            "[yellow]💡 Tip: Install 'readchar' for better navigation[/yellow]\n\n"
            "[dim]pip install readchar[/dim]",
            border_style="yellow",
            box=box.ROUNDED
        ))
    
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
            'icon': '',
            'title': name,
            'description': f"{auth_icon} {display_url}",
            'value': ('chat', agent_id, config)
        })
    
    options.append({
        'icon': '➕',
        'title': 'Add New Agent',
        'description': 'Configure a new AI agent connection',
        'value': ('add', None, None)
    })
    
    options.append({
        'icon': '👋',
        'title': 'Exit',
        'description': 'Close Telminator CLI',
        'value': ('exit', None, None)
    })
    
    selected_index = 0
    
    if has_readchar:
        while True:
            console.clear()
            
            # Header
            console.print(Panel(
                "[bold cyan]Agent Selection[/bold cyan]\n\n"
                "[dim]Use ↑/↓ arrow keys to navigate, Enter to select[/dim]",
                border_style="cyan",
                box=box.DOUBLE
            ))
            console.print()
            
            # Display options
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
        console.print(Panel(
            "[bold cyan]Agent Selection[/bold cyan]",
            border_style="cyan",
            box=box.DOUBLE
        ))
        console.print()
        
        for idx, option in enumerate(options):
            console.print(f"[bold cyan]{idx + 1}.[/bold cyan] {option['icon']} [bold white]{option['title']}[/bold white]")
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
    headers = {}
    # Merge any provided additional headers first
    if additional_headers:
        headers.update(additional_headers)

    if not agent_config:
        return headers

    auth_type = agent_config.get('auth_type', 'none')

    if auth_type == 'api-key' and 'api_key' in agent_config:
        header_name = agent_config.get('api_key_header', 'X-API-Key')
        headers[header_name] = agent_config['api_key']
    elif auth_type == 'bearer' and 'bearer_token' in agent_config:
        headers['Authorization'] = f"Bearer {agent_config['bearer_token']}"
    elif auth_type == 'custom' and 'custom_header' in agent_config:
        custom = agent_config['custom_header']
        headers[custom['name']] = custom['value']

    return headers


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
    agent_name="Agent",
):
    # Prompt with timestamp
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    prompt = Prompt.ask(
        f"[dim]{timestamp}[/dim] [bold blue]👤 You[/bold blue]",
        default=""
    )
    
    # Handle commands
    if not prompt or prompt.lower() in ['quit', 'exit', 'q']:
        console.print()
        console.print(Panel(
            "[bold cyan]👋 Chat session ended[/bold cyan]\n\n"
            "[dim]Thanks for using Telminator![/dim]",
            border_style="cyan",
            box=box.DOUBLE
        ))
        return False, None, None, None
    
    if prompt.lower() in ['switch', 'agents']:
        return False, None, None, 'switch'
    
    if prompt.lower() == 'clear':
        return True, context_id, task_id, 'clear'
    
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
        
        # Beautiful progress indicator
        with Progress(
            SpinnerColumn(spinner_name="dots12"),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            console=console,
            transient=True
        ) as progress:
            progress_task = progress.add_task(f" {agent_name} is thinking...", total=None)
            
            try:
                response_stream = client.send_message_streaming(
                    SendStreamingMessageRequest(id=str(uuid4()), params=payload)
                )
                
                async for result in response_stream:
                    if debug:
                        console.print(f"[dim]Event: {result.root}[/dim]")
                    
                    if isinstance(result.root, JSONRPCErrorResponse):
                        progress.stop()
                        display_api_error({"error": result.root.error})
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
                            progress.update(progress_task, description=f"[cyan]Status: {status_state}[/cyan]")
                        
                        # Working state
                        if status_state == 'working' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts and not agent_responded:
                                progress.stop()
                                timestamp = datetime.now().strftime("%H:%M:%S")
                                console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
                                agent_responded = True
                            
                            for text in texts:
                                console.print(f"[dim italic]{text}[/dim italic]")
                        
                        # Input required
                        elif status_state == 'input-required' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts:
                                if not agent_responded:
                                    progress.stop()
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
                                    agent_responded = True
                                
                                for text in texts:
                                    console.print(text)
                                final_artifact_shown = True
                    
                    elif isinstance(event, Message):
                        if not agent_responded:
                            progress.stop()
                            timestamp = datetime.now().strftime("%H:%M:%S")
                            console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
                        "[bold red]🔐 Authentication Error[/bold red]\n\n"
                        "[yellow]Invalid credentials or service issue.[/yellow]\n\n"
                        "[dim]→ Check your API key\n"
                        "→ Verify agent URL is correct[/dim]",
                        title="[bold red]STREAM ERROR[/bold red]",
                        border_style="red",
                        box=box.DOUBLE
                    ))
                else:
                    console.print(Panel(
                        f"[bold red]Stream Error[/bold red]\n\n"
                        f"[yellow]{str(e)}[/yellow]",
                        border_style="red",
                        box=box.DOUBLE
                    ))
                
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return False, context_id, task_id, None
        
        if agent_responded:
            console.print()
        
        # Fetch task if no response
        if task_id and not agent_responded:
            if debug:
                console.print("[dim]Fetching task results...[/dim]")
            
            try:
                taskResultResponse = await client.get_task(
                    GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task_id))
                )
                
                if isinstance(taskResultResponse.root, JSONRPCErrorResponse):
                    display_api_error({"error": taskResultResponse.root.error})
                    return False, context_id, task_id, None
                
                taskResult = taskResultResponse.root.result
                
                if hasattr(taskResult, 'status') and hasattr(taskResult.status, 'message'):
                    msg = taskResult.status.message
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
        with Progress(
            SpinnerColumn(spinner_name="arc"),
            TextColumn("[bold cyan]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task(f" {agent_name} is processing...", total=None)
            
            try:
                event = await client.send_message(
                    SendMessageRequest(id=str(uuid4()), params=payload)
                )
                event = event.root.result
            except httpx.HTTPStatusError as e:
                await handle_http_error(e, "message send")
                return False, context_id, task_id, None
            except Exception as e:
                console.print(Panel(
                    f"[bold red]Request Failed[/bold red]\n\n"
                    f"[yellow]{str(e)}[/yellow]",
                    border_style="red",
                    box=box.DOUBLE
                ))
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
            timestamp = datetime.now().strftime("%H:%M:%S")
            console.print(f"\n[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
            texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
            for text in texts:
                console.print(text)
            console.print()

    if taskResult:
        state = TaskState(taskResult.status.state)
        
        if state.name == TaskState.input_required.name:
            if debug:
                console.print("[dim]Agent requires additional input...[/dim]")
            return await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                task_id,
                context_id,
                debug,
                agent_name,
            )
        
        return True, context_id, task_id, None
    
    return True, context_id, task_id, None


@click.command()
@click.argument('agent_url', required=False, metavar='URL')
@click.option('--agent', 'agent_option', help='Agent URL')
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
    """ TELMINATOR - A2A Multi-Agent CLI
    
    Connect and chat with AI agents using A2A protocol
    """
    
    # Clear screen and show animated banner
    console.clear()
    animated_banner()
    
    agent = agent_option or agent_url
    
    # Reset config
    if reset:
        with Progress(
            SpinnerColumn(),
            TextColumn("[yellow]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("Resetting configuration...", total=None)
            time.sleep(0.5)
            
            if CONFIG_FILE.exists():
                CONFIG_FILE.unlink()
            if ENV_FILE.exists():
                ENV_FILE.unlink()
        
        console.print(Panel(
            "[green]✓ Configuration reset successfully[/green]\n\n"
            "[dim]All saved agents and settings have been cleared[/dim]",
            title="[bold green]✓ RESET COMPLETE[/bold green]",
            border_style="green",
            box=box.DOUBLE
        ))
        return
    
    # Load agents
    agents_config = load_agents_config()
    
    # List agents
    if list_agents:
        if not agents_config:
            console.print(Panel(
                "[yellow]📭 No agents configured yet[/yellow]\n\n"
                "[dim]Add your first agent with:[/dim]\n"
                "[cyan]uv run . --add --agent <URL>[/cyan]",
                border_style="yellow",
                box=box.ROUNDED
            ))
        else:
            table = Table(
                title="[bold cyan] Configured Agents[/bold cyan]",
                box=box.DOUBLE,
                border_style="cyan",
                show_header=True,
                header_style="bold cyan"
            )
            
            table.add_column("#", style="dim", width=4)
            table.add_column("Name", style="bold white")
            table.add_column("URL", style="cyan")
            table.add_column("Auth", style="yellow")
            table.add_column("ID", style="dim")
            
            for idx, (agent_id, config) in enumerate(agents_config.items(), 1):
                name = config.get('name', 'Unknown')
                url = config['url']
                auth_type = config.get('auth_type', 'none')
                auth_icon = "🔒" if auth_type != 'none' else "🔓"
                
                table.add_row(
                    str(idx),
                    name,
                    url[:40] + '...' if len(url) > 40 else url,
                    f"{auth_icon} {auth_type}",
                    agent_id
                )
            
            console.print()
            console.print(table)
            console.print()
        return
    
    # Remove agent
    if remove:
        if remove in agents_config:
            removed_name = agents_config[remove].get('name', remove)
            del agents_config[remove]
            save_agents_config(agents_config)
            
            console.print(Panel(
                f"[green]✓ Agent '{removed_name}' removed successfully[/green]\n\n"
                f"[dim]ID: {remove}[/dim]",
                title="[bold green]✓ REMOVED[/bold green]",
                border_style="green",
                box=box.DOUBLE
            ))
        else:
            console.print(Panel(
                f"[red]✗ Agent ID '{remove}' not found[/red]\n\n"
                "[dim]Use --list to see available agents[/dim]",
                title="[bold red]✗ NOT FOUND[/bold red]",
                border_style="red",
                box=box.DOUBLE
            ))
        return
    
    # Add new agent
    if add:
        if not agent:
            console.print(Panel(
                "[bold cyan]➕ Add New Agent[/bold cyan]\n\n"
                "[dim]Enter the agent's base URL[/dim]",
                border_style="cyan",
                box=box.ROUNDED
            ))
            agent = Prompt.ask("\n[cyan]Agent URL[/cyan]")
        
        agent_config = await setup_agent_auth(agent)
        if not agent_config:
            return
            
        agent_id = str(uuid4())[:8]
        agents_config[agent_id] = agent_config
        save_agents_config(agents_config)
        
        console.print()
        console.print(Panel(
            f"[green]✓ Agent saved successfully[/green]\n\n"
            f"[bold white]Name:[/bold white] {agent_config['name']}\n"
            f"[bold white]ID:[/bold white] {agent_id}\n"
            f"[bold white]URL:[/bold white] {agent_config['url']}",
            title="[bold green]✓ AGENT ADDED[/bold green]",
            border_style="green",
            box=box.DOUBLE
        ))
        
        if not Confirm.ask("\n[cyan]💬 Start chatting now?[/cyan]", default=True):
            return
        
        selected_agent_id = agent_id
        selected_agent_config = agent_config
    
    # Select from existing agents
    elif not agent and agents_config:
        result = select_agent_interactive(agents_config)
        if not result:
            console.print("[yellow]No agent selected[/yellow]")
            return
        
        action, agent_id, agent_config = result
        
        if action == 'exit':
            console.print()
            console.print(Panel(
                "[bold cyan]👋 Thanks for using Telminator![/bold cyan]\n\n"
                "[dim]Come back soon![/dim]",
                border_style="cyan",
                box=box.DOUBLE
            ))
            return
        elif action == 'add':
            agent = Prompt.ask("\n[cyan]Agent URL[/cyan]")
            agent_config = await setup_agent_auth(agent)
            if not agent_config:
                return
                
            new_agent_id = str(uuid4())[:8]
            agents_config[new_agent_id] = agent_config
            save_agents_config(agents_config)
            
            console.print(Panel(
                f"[green]✓ Agent saved successfully[/green]\n\n"
                f"[bold]ID:[/bold] {new_agent_id}",
                border_style="green",
                box=box.DOUBLE
            ))
            
            if not Confirm.ask("\n[cyan]💬 Start chatting now?[/cyan]", default=True):
                return
            
            selected_agent_id = new_agent_id
            selected_agent_config = agent_config
        elif action == 'chat':
            selected_agent_id = agent_id
            selected_agent_config = agent_config
        else:
            return
    
    # Direct URL or first time
    elif agent:
        agent_config = await setup_agent_auth(agent)
        if not agent_config:
            return
        
        if Confirm.ask("\n[cyan]💾 Save this agent for future use?[/cyan]", default=True):
            agent_id = str(uuid4())[:8]
            agents_config[agent_id] = agent_config
            save_agents_config(agents_config)
            console.print(Panel(
                f"[green]✓ Saved (ID: {agent_id})[/green]",
                border_style="green",
                box=box.ROUNDED
            ))
            selected_agent_id = agent_id
        else:
            selected_agent_id = 'temp'
        
        selected_agent_config = agent_config
    
    else:
        console.print(Panel(
            "[bold yellow]🎯 First Time Setup[/bold yellow]\n\n"
            "[dim]No agents configured yet. Let's add your first one![/dim]",
            border_style="yellow",
            box=box.DOUBLE
        ))
        
        agent = Prompt.ask("\n[cyan]Agent URL[/cyan]")
        agent_config = await setup_agent_auth(agent)
        if not agent_config:
            return
            
        agent_id = str(uuid4())[:8]
        agents_config[agent_id] = agent_config
        save_agents_config(agents_config)
        
        console.print(Panel(
            f"[green]✓ First agent configured![/green]\n\n"
            f"[bold]ID:[/bold] {agent_id}",
            border_style="green",
            box=box.DOUBLE
        ))
        
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
        # Connect to agent with animation
        console.print()
        with Progress(
            SpinnerColumn(spinner_name="bouncingBar"),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("🔗 Establishing connection...", total=100)
            
            try:
                for i in range(0, 100, 20):
                    await asyncio.sleep(0.1)
                    progress.update(task, advance=20)
                
                card_resolver = A2ACardResolver(httpx_client, agent_url, agent_card_path="/.well-known/agent.json")
                card = await card_resolver.get_agent_card()
                progress.update(task, completed=100)
                
            except httpx.HTTPStatusError as e:
                if await handle_http_error(e, "connection"):
                    return
            except Exception as e:
                console.print(Panel(
                    f"[bold red]✗ Connection failed[/bold red]\n\n"
                    f"[yellow]{str(e)}[/yellow]",
                    title="[bold red]CONNECTION ERROR[/bold red]",
                    border_style="red",
                    box=box.DOUBLE
                ))
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return

        console.print(Panel(
            "[bold green]✓ Connected successfully![/bold green]",
            border_style="green",
            box=box.ROUNDED
        ))
        console.print()
        
        # Display agent info with beautiful formatting
        info_panel = Table.grid(padding=(0, 2))
        info_panel.add_column(style="bold cyan", justify="right")
        info_panel.add_column(style="white")
        
        info_panel.add_row(" Agent", f"[bold]{card.name or 'Unknown'}[/bold]")
        
        if card.description:
            desc = card.description[:80] + '...' if len(card.description) > 80 else card.description
            info_panel.add_row("📝 About", desc)
        
        info_panel.add_row("🔢 Version", card.version or "N/A")
        info_panel.add_row("⚡ Streaming", "✓ Enabled" if card.capabilities.streaming else "✗ Disabled")
        
        if card.skills:
            skills_count = len(card.skills)
            info_panel.add_row("🛠️  Skills", f"{skills_count} available")
        
        console.print(Panel(
            info_panel,
            title="[bold cyan]📊 AGENT INFORMATION[/bold cyan]",
            border_style="cyan",
            box=box.DOUBLE,
            padding=(1, 2)
        ))

        if debug and card.skills:
            console.print("\n[bold cyan]🛠️  Available Skills:[/bold cyan]")
            for idx, skill in enumerate(card.skills[:5], 1):
                console.print(f"  [cyan]{idx}.[/cyan] {skill.name}")
            if len(card.skills) > 5:
                console.print(f"  [dim]... and {len(card.skills) - 5} more[/dim]")

        if debug and headers:
            console.print("\n[bold cyan]🔧 Request Headers:[/bold cyan]")
            for key, value in headers.items():
                display_value = "***" if any(x in key.lower() for x in ['token', 'key', 'auth']) else value
                console.print(f"  [cyan]{key}:[/cyan] {display_value}")

        # Push notifications setup
        notif_receiver_parsed = urllib.parse.urlparse(push_notification_receiver)
        notification_receiver_host = notif_receiver_parsed.hostname
        notification_receiver_port = notif_receiver_parsed.port

        if use_push_notifications:
            from hosts.cli.push_notification_listener import PushNotificationListener
            
            with Progress(
                SpinnerColumn(),
                TextColumn("[yellow]{task.description}"),
                console=console,
                transient=True
            ) as progress:
                task = progress.add_task("Starting push notification listener...", total=None)
                push_notification_listener = PushNotificationListener(
                    host=notification_receiver_host,
                    port=notification_receiver_port,
                )
                push_notification_listener.start()
            
            console.print(Panel(
                "[green]✓ Push notifications enabled[/green]",
                border_style="green",
                box=box.ROUNDED
            ))

        client = A2AClient(httpx_client, agent_card=card, url=agent_url.rstrip('/'))
        continue_loop = True
        streaming = card.capabilities.streaming
        context_id = session if session > 0 else uuid4().hex
        
        # Chat session header
        console.print()
        console.print(Panel(
            f"[bold cyan]💬 Chat Session Started[/bold cyan]\n\n"
            f"[dim]Session ID: {context_id[:16]}...[/dim]\n"
            f"[dim]Agent: {card.name}[/dim]\n\n"
            f"[yellow]Commands:[/yellow]\n"
            f"[dim]• Type your message to chat\n"
            f"• 'exit' or 'quit' to leave\n"
            f"• 'switch' to change agent\n"
            f"• 'clear' to clear screen[/dim]",
            border_style="cyan",
            box=box.DOUBLE
        ))
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
                card.name,
            )
            
            # Handle commands
            if command == 'switch':
                if len(agents_config) > 1:
                    console.print(Panel(
                        "[yellow]🔄 To switch agents:[/yellow]\n\n"
                        "[cyan]uv run .[/cyan]\n\n"
                        "[dim]Then select a different agent from the menu[/dim]",
                        border_style="yellow",
                        box=box.ROUNDED
                    ))
                else:
                    console.print(Panel(
                        "[yellow]⚠️  Only one agent configured[/yellow]\n\n"
                        "[dim]Add more agents with:[/dim]\n"
                        "[cyan]uv run . --add[/cyan]",
                        border_style="yellow",
                        box=box.ROUNDED
                    ))
                continue_loop = False
            
            elif command == 'clear':
                console.clear()
                animated_banner()

            if history and continue_loop and task_id:
                console.print()
                console.print(Panel(
                    "[bold cyan]📜 CONVERSATION HISTORY[/bold cyan]",
                    border_style="cyan",
                    box=box.DOUBLE
                ))
                
                try:
                    task_response = await client.get_task({'id': task_id, 'historyLength': 10})
                    
                    if hasattr(task_response.root, 'result') and hasattr(task_response.root.result, 'history'):
                        for idx, msg in enumerate(task_response.root.result.history):
                            role_color = "blue" if msg.role == "user" else "green"
                            role_icon = "👤" if msg.role == "user" else ""
                            role_label = "You" if msg.role == "user" else card.name
                            
                            console.print(f"\n[bold {role_color}]{role_icon} {role_label}:[/bold {role_color}]")
                            
                            for part in msg.parts:
                                if hasattr(part, 'text'):
                                    console.print(f"  {part.text}")
                            
                            if idx < len(task_response.root.result.history) - 1:
                                console.print("[dim]" + "─" * 60 + "[/dim]")
                    
                    console.print()
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
    agent_name="Agent",
):
    # Prompt with timestamp
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    prompt = Prompt.ask(
        f"[dim]{timestamp}[/dim] [bold blue]👤 You[/bold blue]",
        default=""
    )
    
    # Handle commands
    if not prompt or prompt.lower() in ['quit', 'exit', 'q']:
        console.print()
        console.print(Panel(
            "[bold cyan]👋 Chat session ended[/bold cyan]\n\n"
            "[dim]Thanks for using Telminator![/dim]",
            border_style="cyan",
            box=box.DOUBLE
        ))
        return False, None, None, None
    
    if prompt.lower() in ['switch', 'agents']:
        return False, None, None, 'switch'
    
    if prompt.lower() == 'clear':
        return True, context_id, task_id, 'clear'
    
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
        
        # Beautiful progress indicator
        with Progress(
            SpinnerColumn(spinner_name="dots12"),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            console=console,
            transient=True
        ) as progress:
            progress_task = progress.add_task(f" {agent_name} is thinking...", total=None)
            
            try:
                response_stream = client.send_message_streaming(
                    SendStreamingMessageRequest(id=str(uuid4()), params=payload)
                )
                
                async for result in response_stream:
                    if debug:
                        console.print(f"[dim]Event: {result.root}[/dim]")
                    
                    if isinstance(result.root, JSONRPCErrorResponse):
                        progress.stop()
                        display_api_error({"error": result.root.error})
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
                            progress.update(progress_task, description=f"[cyan]Status: {status_state}[/cyan]")
                        
                        # Working state
                        if status_state == 'working' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts and not agent_responded:
                                progress.stop()
                                timestamp = datetime.now().strftime("%H:%M:%S")
                                console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
                                agent_responded = True
                            
                            for text in texts:
                                console.print(f"[dim italic]{text}[/dim italic]")
                        
                        # Input required
                        elif status_state == 'input-required' and hasattr(event, 'status') and hasattr(event.status, 'message') and event.status.message:
                            msg = event.status.message
                            texts = extract_text_from_parts(msg.parts if hasattr(msg, 'parts') else [])
                            
                            if texts:
                                if not agent_responded:
                                    progress.stop()
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
                                    agent_responded = True
                                
                                for text in texts:
                                    console.print(text)
                                final_artifact_shown = True
                    
                    elif isinstance(event, Message):
                        if not agent_responded:
                            progress.stop()
                            timestamp = datetime.now().strftime("%H:%M:%S")
                            console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
                        "[bold red]🔐 Authentication Error[/bold red]\n\n"
                        "[yellow]Invalid credentials or service issue.[/yellow]\n\n"
                        "[dim]→ Check your API key\n"
                        "→ Verify agent URL is correct[/dim]",
                        title="[bold red]STREAM ERROR[/bold red]",
                        border_style="red",
                        box=box.DOUBLE
                    ))
                else:
                    console.print(Panel(
                        f"[bold red]Stream Error[/bold red]\n\n"
                        f"[yellow]{str(e)}[/yellow]",
                        border_style="red",
                        box=box.DOUBLE
                    ))
                
                if debug:
                    import traceback
                    console.print(f"[dim]{traceback.format_exc()}[/dim]")
                return False, context_id, task_id, None
        
        if agent_responded:
            console.print()
        
        # Fetch task if no response
        if task_id and not agent_responded:
            if debug:
                console.print("[dim]Fetching task results...[/dim]")
            
            try:
                taskResultResponse = await client.get_task(
                    GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task_id))
                )
                
                if isinstance(taskResultResponse.root, JSONRPCErrorResponse):
                    display_api_error({"error": taskResultResponse.root.error})
                    return False, context_id, task_id, None
                
                taskResult = taskResultResponse.root.result
                
                if hasattr(taskResult, 'status') and hasattr(taskResult.status, 'message'):
                    msg = taskResult.status.message
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    console.print(f"[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
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
        with Progress(
            SpinnerColumn(spinner_name="arc"),
            TextColumn("[bold cyan]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task(f" {agent_name} is processing...", total=None)
            
            try:
                event = await client.send_message(
                    SendMessageRequest(id=str(uuid4()), params=payload)
                )
                event = event.root.result
            except httpx.HTTPStatusError as e:
                await handle_http_error(e, "message send")
                return False, context_id, task_id, None
            except Exception as e:
                console.print(Panel(
                    f"[bold red]Request Failed[/bold red]\n\n"
                    f"[yellow]{str(e)}[/yellow]",
                    border_style="red",
                    box=box.DOUBLE
                ))
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
            timestamp = datetime.now().strftime("%H:%M:%S")
            console.print(f"\n[dim]{timestamp}[/dim] [bold green] {agent_name}[/bold green]")
            texts = extract_text_from_parts(event.parts if hasattr(event, 'parts') else [])
            for text in texts:
                console.print(text)
            console.print()

    if taskResult:
        state = TaskState(taskResult.status.state)
        
        if state.name == TaskState.input_required.name:
            if debug:
                console.print("[dim]Agent requires additional input...[/dim]")
            return await completeTask(
                client,
                streaming,
                use_push_notifications,
                notification_receiver_host,
                notification_receiver_port,
                task_id,
                context_id,
                debug,
                agent_name,
            )
        
        return True, context_id, task_id, None
    
    return True, context_id, task_id, None


if __name__ == '__main__':
    asyncio.run(cli())
