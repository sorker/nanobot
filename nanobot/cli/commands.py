"""CLI commands for nanobot."""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from nanobot import __version__, __logo__

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nanobot configuration and workspace."""
    from nanobot.config.loader import get_config_path, save_config
    from nanobot.config.schema import Config
    from nanobot.utils.helpers import get_workspace_path
    
    config_path = get_config_path()
    
    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()
    
    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")
    
    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")
    
    # Create default bootstrap files
    _create_workspace_templates(workspace)
    
    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]nanobot agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")




def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
        "SOUL.md": """# Soul

I am nanobot, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
    }
    
    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")
    
    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nanobot gateway."""
    from nanobot.config.loader import load_config, get_data_dir
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.agent.loop import AgentLoop
    from nanobot.channels.manager import ChannelManager
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    
    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)
    
    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")
    
    config = load_config()
    
    # Create components
    bus = MessageBus()
    
    # Create provider (supports OpenRouter, Anthropic, OpenAI, Bedrock, Ollama, etc.)
    api_key = config.get_api_key()
    api_base = config.get_api_base()
    model = config.agents.defaults.model
    is_bedrock = model.startswith("bedrock/")
    is_ollama = (
        model.startswith("ollama/") or 
        (api_base and ("ollama" in api_base.lower() or ":11434" in api_base))
    )

    if not api_key and not is_bedrock and not is_ollama:
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under one of the providers:")
        console.print("  - providers.openrouter.apiKey (for OpenRouter)")
        console.print("  - providers.anthropic.apiKey (for Anthropic Claude)")
        console.print("  - providers.openai.apiKey (for OpenAI)")
        console.print("  - providers.ollama.apiBase (for local Ollama, e.g., http://localhost:11434)")
        console.print("  - providers.gemini.apiKey (for Google Gemini)")
        console.print("  - providers.zhipu.apiKey (for Zhipu AI)")
        console.print("  - providers.dashscope.apiKey (for Alibaba DashScope/Qwen)")
        console.print("  - providers.groq.apiKey (for Groq)")
        raise typer.Exit(1)
    
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.agents.defaults.model
    )
    
    # Create agent
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        oss_config=config.tools.oss,
    )
    
    # Create cron service
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}"
        )
        # Optionally deliver to channel
        if job.payload.deliver and job.payload.to:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "whatsapp",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, on_job=on_cron_job)
    
    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(prompt, session_key="heartbeat")
    
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )
    
    # Create channel manager
    channels = ChannelManager(config, bus)
    
    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")
    
    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")
    
    console.print(f"[green]✓[/green] Heartbeat: every 30m")
    
    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()
    
    asyncio.run(run())




# ============================================================================
# SSE Server
# ============================================================================


@app.command()
def sse(
    port: int = typer.Option(18790, "--port", "-p", help="SSE server port"),
    host: str = typer.Option("0.0.0.0", "--host", help="SSE server host"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the SSE unified Agent server."""
    from nanobot.config.loader import load_config
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.agent.loop import AgentLoop
    from nanobot.sse.handler import SSEHandler
    from nanobot.sse.app import create_app

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot SSE server on {host}:{port}...")

    config = load_config()

    # Validate API key
    api_key = config.get_api_key()
    api_base = config.get_api_base()
    model = config.agents.defaults.model
    is_bedrock = model.startswith("bedrock/")
    is_ollama = (
        model.startswith("ollama/")
        or (api_base and ("ollama" in api_base.lower() or ":11434" in api_base))
    )

    if not api_key and not is_bedrock and not is_ollama:
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under one of the providers.")
        raise typer.Exit(1)

    # Build components
    bus = MessageBus()
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=model,
    )
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        oss_config=config.tools.oss,
    )
    handler = SSEHandler(agent)
    fastapi_app = create_app(handler)

    console.print(f"[green]✓[/green] Model: {model}")
    console.print(f"[green]✓[/green] Endpoint: POST http://{host}:{port}/v1/chat/completions")
    console.print(f"[green]✓[/green] Health: GET  http://{host}:{port}/health")

    import uvicorn
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from nanobot.config.loader import load_config
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.agent.loop import AgentLoop
    
    config = load_config()
    
    api_key = config.get_api_key()
    api_base = config.get_api_base()
    model = config.agents.defaults.model
    is_bedrock = model.startswith("bedrock/")

    if not api_key and not is_bedrock:
        console.print("[red]Error: No API key configured.[/red]")
        raise typer.Exit(1)

    bus = MessageBus()
    provider = LiteLLMProvider(
        api_key=api_key,
        api_base=api_base,
        default_model=config.agents.defaults.model
    )
    
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        oss_config=config.tools.oss,
    )
    
    if message:
        # Single message mode
        async def run_once():
            response = await agent_loop.process_direct(message, session_id)
            console.print(f"\n{__logo__} {response}")
        
        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")
        
        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue
                    
                    response = await agent_loop.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break
        
        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa.bridge_url
    )

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess
    
    # User's bridge location
    user_bridge = Path.home() / ".nanobot" / "bridge"
    
    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge
    
    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)
    
    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)
    
    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge
    
    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)
    
    console.print(f"{__logo__} Setting up bridge...")
    
    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))
    
    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)
        
        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)
    
    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess
    
    bridge_dir = _get_bridge_dir()
    
    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")
    
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    jobs = service.list_jobs(include_disabled=all)
    
    if not jobs:
        console.print("No scheduled jobs.")
        return
    
    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")
    
    import time
    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"
        
        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time
        
        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"
        
        table.add_row(job.id, job.name, sched, status, next_run)
    
    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule
    
    # Determine schedule type
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )
    
    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
):
    """Enable or disable a job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
):
    """Manually run a job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    async def run():
        return await service.run_job(job_id, force=force)
    
    if asyncio.run(run()):
        console.print(f"[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Plugin Marketplace Commands
# ============================================================================


plugin_app = typer.Typer(help="Manage plugins and skills from marketplace")
app.add_typer(plugin_app, name="plugin")


marketplace_app = typer.Typer(help="Plugin marketplace operations")
plugin_app.add_typer(marketplace_app, name="marketplace")


@marketplace_app.command("add")
def marketplace_add(
    repo: str = typer.Argument(..., help="GitHub repository (e.g., 'anthropics/skills')"),
    skill_name: str = typer.Option(None, "--name", "-n", help="Specific skill name to install (optional)"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing skills"),
):
    """Install skills from a GitHub repository."""
    import tempfile
    import zipfile
    import shutil
    
    from nanobot.config.loader import load_config
    from nanobot.utils.helpers import get_skills_path
    
    # Parse repository path
    if "/" not in repo:
        console.print(f"[red]Error: Invalid repository format. Expected 'owner/repo'[/red]")
        raise typer.Exit(1)
    
    owner, repo_name = repo.split("/", 1)
    
    # Get workspace skills directory
    config = load_config()
    skills_dir = get_skills_path(config.workspace_path)
    
    console.print(f"{__logo__} Installing from [cyan]{repo}[/cyan]...")
    
    # Download repository
    try:
        import httpx
        
        # GitHub API: get default branch
        api_url = f"https://api.github.com/repos/{owner}/{repo_name}"
        with httpx.Client(timeout=30.0) as client:
            try:
                response = client.get(api_url)
                response.raise_for_status()
                repo_data = response.json()
                default_branch = repo_data.get("default_branch", "main")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    console.print(f"[red]Error: Repository {repo} not found[/red]")
                    raise typer.Exit(1)
                raise
        
        # Download zip file
        zip_url = f"https://github.com/{owner}/{repo_name}/archive/refs/heads/{default_branch}.zip"
        console.print(f"  Downloading from GitHub...")
        
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(zip_url)
                response.raise_for_status()
                tmp_file.write(response.content)
                tmp_file_path = tmp_file.name
        
        # Extract zip
        console.print(f"  Extracting...")
        with tempfile.TemporaryDirectory() as tmp_dir:
            extract_path = Path(tmp_dir)
            with zipfile.ZipFile(tmp_file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            
            # Find the extracted repository directory
            extracted_repo = None
            for item in extract_path.iterdir():
                if item.is_dir() and repo_name in item.name:
                    extracted_repo = item
                    break
            
            if not extracted_repo:
                console.print(f"[red]Error: Could not find extracted repository[/red]")
                raise typer.Exit(1)
            
            # Find skills in the repository
            # Look for directories with SKILL.md files
            skills_found = []
            for item in extracted_repo.rglob("SKILL.md"):
                skill_dir = item.parent
                skill_name_found = skill_dir.name
                skills_found.append((skill_name_found, skill_dir))
            
            if not skills_found:
                console.print(f"[yellow]Warning: No skills found in repository (no SKILL.md files)[/yellow]")
                raise typer.Exit(1)
            
            # Filter by skill_name if specified
            if skill_name:
                skills_found = [(name, path) for name, path in skills_found if name == skill_name]
                if not skills_found:
                    console.print(f"[red]Error: Skill '{skill_name}' not found in repository[/red]")
                    raise typer.Exit(1)
            
            # Install skills
            installed = []
            for skill_name_found, skill_dir in skills_found:
                target_dir = skills_dir / skill_name_found
                
                if target_dir.exists() and not force:
                    console.print(f"  [yellow]Skipping {skill_name_found} (already exists, use --force to overwrite)[/yellow]")
                    continue
                
                # Copy skill directory
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                
                shutil.copytree(skill_dir, target_dir)
                installed.append(skill_name_found)
                console.print(f"  [green]✓[/green] Installed {skill_name_found}")
        
        # Cleanup
        Path(tmp_file_path).unlink()
        
        if installed:
            console.print(f"\n[green]✓[/green] Successfully installed {len(installed)} skill(s): {', '.join(installed)}")
        else:
            console.print(f"\n[yellow]No new skills installed[/yellow]")
            
    except httpx.RequestError as e:
        console.print(f"[red]Error: Network error - {e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


@marketplace_app.command("list")
def marketplace_list():
    """List installed skills from workspace."""
    from nanobot.config.loader import load_config
    from nanobot.agent.skills import SkillsLoader
    
    config = load_config()
    skills_loader = SkillsLoader(config.workspace_path)
    
    # Get all skills (including unavailable)
    all_skills = skills_loader.list_skills(filter_unavailable=False)
    # Get available skills to check availability
    available_skills = {s["name"] for s in skills_loader.list_skills(filter_unavailable=True)}
    
    if not all_skills:
        console.print("No skills installed.")
        return
    
    table = Table(title="Installed Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Available", style="yellow")
    table.add_column("Description")
    table.add_column("Missing Requirements", style="red")
    
    for skill in all_skills:
        meta = skills_loader.get_skill_metadata(skill["name"])
        desc = meta.get("description", "") if meta else ""
        if len(desc) > 50:
            desc = desc[:47] + "..."
        
        available = "✓" if skill["name"] in available_skills else "✗"
        source = skill["source"]
        
        # Get missing requirements for unavailable skills
        missing = ""
        if skill["name"] not in available_skills:
            skill_meta = skills_loader._get_skill_meta(skill["name"])
            missing = skills_loader._get_missing_requirements(skill_meta)
            if missing and len(missing) > 40:
                missing = missing[:37] + "..."
        
        table.add_row(skill["name"], source, available, desc, missing)
    
    console.print(table)


@marketplace_app.command("remove")
def marketplace_remove(
    skill_name: str = typer.Argument(..., help="Skill name to remove"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
):
    """Remove a skill from workspace."""
    import shutil
    from nanobot.config.loader import load_config
    from nanobot.utils.helpers import get_skills_path
    from nanobot.agent.skills import SkillsLoader
    
    config = load_config()
    skills_dir = get_skills_path(config.workspace_path)
    skills_loader = SkillsLoader(config.workspace_path)
    
    # Check if skill exists in workspace
    skill_path = skills_dir / skill_name
    if not skill_path.exists():
        # Check if it's a builtin skill
        all_skills = skills_loader.list_skills(filter_unavailable=False)
        skill_info = next((s for s in all_skills if s["name"] == skill_name), None)
        
        if skill_info:
            if skill_info["source"] == "builtin":
                console.print(f"[yellow]Warning: '{skill_name}' is a built-in skill and cannot be removed.[/yellow]")
                console.print("Built-in skills are part of nanobot and cannot be uninstalled.")
                raise typer.Exit(1)
            else:
                console.print(f"[red]Error: Skill '{skill_name}' not found in workspace.[/red]")
                raise typer.Exit(1)
        else:
            console.print(f"[red]Error: Skill '{skill_name}' not found.[/red]")
            raise typer.Exit(1)
    
    # Confirm deletion
    if not force:
        meta = skills_loader.get_skill_metadata(skill_name)
        desc = meta.get("description", "") if meta else "No description"
        console.print(f"\nSkill: [cyan]{skill_name}[/cyan]")
        console.print(f"Description: {desc}")
        console.print(f"Location: {skill_path}")
        
        if not typer.confirm(f"\nAre you sure you want to remove '{skill_name}'?"):
            console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)
    
    # Remove the skill directory
    try:
        shutil.rmtree(skill_path)
        console.print(f"[green]✓[/green] Removed skill '{skill_name}'")
    except Exception as e:
        console.print(f"[red]Error: Failed to remove skill - {e}[/red]")
        raise typer.Exit(1)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show nanobot status."""
    from nanobot.config.loader import load_config, get_config_path

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        console.print(f"Model: {config.agents.defaults.model}")
        
        # Check API keys
        has_openrouter = bool(config.providers.openrouter.api_key)
        has_anthropic = bool(config.providers.anthropic.api_key)
        has_openai = bool(config.providers.openai.api_key)
        has_gemini = bool(config.providers.gemini.api_key)
        has_vllm = bool(config.providers.vllm.api_base)
        
        console.print(f"OpenRouter API: {'[green]✓[/green]' if has_openrouter else '[dim]not set[/dim]'}")
        console.print(f"Anthropic API: {'[green]✓[/green]' if has_anthropic else '[dim]not set[/dim]'}")
        console.print(f"OpenAI API: {'[green]✓[/green]' if has_openai else '[dim]not set[/dim]'}")
        console.print(f"Gemini API: {'[green]✓[/green]' if has_gemini else '[dim]not set[/dim]'}")
        vllm_status = f"[green]✓ {config.providers.vllm.api_base}[/green]" if has_vllm else "[dim]not set[/dim]"
        console.print(f"vLLM/Local: {vllm_status}")


if __name__ == "__main__":
    app()
