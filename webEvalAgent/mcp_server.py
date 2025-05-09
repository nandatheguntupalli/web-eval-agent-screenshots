#!/usr/bin/env python3

import asyncio
import os
import argparse
import traceback
import uuid
from enum import Enum
import subprocess
import json
from pathlib import Path
import requests

from webEvalAgent.src.utils import stop_log_server
import json
import sys
from typing import Any, Dict, List, Union
from webEvalAgent.src.log_server import send_log

# Set the API key to a fake key to avoid error in backend
os.environ["ANTHROPIC_API_KEY"] = 'not_a_real_key'
os.environ["ANONYMIZED_TELEMETRY"] = 'false'

# MCP imports
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import TextContent
# Removing the problematic import
# from mcp.server.tool import Tool, register_tool

# Import our modules
from webEvalAgent.src.browser_manager import PlaywrightBrowserManager
# from webEvalAgent.src.browser_utils import cleanup_resources # Removed import
from webEvalAgent.src.api_utils import validate_api_key
from webEvalAgent.src.tool_handlers import handle_web_evaluation, handle_setup_browser_state

# MCP server modules
from webEvalAgent.src.browser_utils import handle_browser_input
from webEvalAgent.src.log_server import start_log_server, open_log_dashboard

# Stop any existing log server to avoid conflicts
# This doesn't start a new server, just ensures none is running
stop_log_server()

# Create the MCP server
mcp = FastMCP("Operative")

# Define the browser tools
class BrowserTools(str, Enum):
    WEB_EVAL_AGENT = "web_eval_agent"
    SETUP_BROWSER_STATE = "setup_browser_state"  # Add new tool enum

# Parse command line arguments (keeping the parser for potential future arguments)
# parser = argparse.ArgumentParser(description='Run the MCP server with browser debugging capabilities')
# args = parser.parse_args()

# --- Start of new/modified functions ---

CONFIG_DIR = Path.home() / ".operative"
CONFIG_FILE = CONFIG_DIR / "config.json"
OPERATIVE_API_KEY_HOLDER = {"key": None} # Global holder for the validated API key

# ANSI color and formatting codes
RED = '\033[0;31m'
GREEN = '\033[0;32m'
BLUE = '\033[0;34m'
YELLOW = '\033[1;33m'
NC = '\033[0m' # No Color
BOLD = '\033[1m'

# ASCII Art for startup logo
OPERATIVE_LOGO = """                                    $$$$                                    
                                 $$$    $$$                                 
                              $$$          $$$                              
                           $$$     $$$$$$     $$$                           
                        $$$     $$$  $$  $$$     $$$c                       
                    c$$$     $$$     $$     $$$     $$$$                    
                   $$$$      $$$x    $$     $$$      $$$$                   
                   $$  $$$      >$$$ $$ ;$$$      $$$  $$                   
                   $$     $$$       $$$$8      $$$     $$                   
                   $$        $$$            $$$        $$                   
                   $$   $$$     $$$$     $$$     $$$   $$                   
                   $$   $  $$$     I$$$$$     $$$  $   $$                   
                   $$   $     $$$    $$    $$$     $   $$                   
                   $$   $     $$$$   $$   $$$$     $   $$                   
                   $$   $  $$$   $   $$   $   $$$  $   $$                   
                   $$   $$$      $   $$   $      $$$   $$                   
                   $$     $$$    $   $$   $    $$$     $$                   
                    $$$      $$$ $   $$   $ $$$      $$$                    
                       $$$      $$   $$   $$      $$$                       
                          $$$        $$        $$$                          
                             $$$     $$     $$$                             
                                $$$  $$  $$$                                
                                   $$$$$$                                   
"""

def ensure_playwright_browsers():
    """Checks and installs Playwright browsers if necessary."""
    try:
        send_log(f"{BOLD}Checking and installing Playwright browsers if necessary...{NC}", "🚀")
        # Using playwright's Python API to check/install is more robust if available.
        # For now, calling the CLI command.
        # Ensure playwright is in the path for uvx environment.
        process = subprocess.run(["playwright", "install", "--with-deps"], capture_output=True, text=True, check=False, timeout=300)
        if process.returncode == 0:
            send_log(f"{GREEN}✓ Playwright browsers are installed successfully{NC}", "✅")
            if process.stdout:
                send_log(f"Playwright install STDOUT: {process.stdout.strip()}", "⚙️")
            if process.stderr:
                send_log(f"Playwright install STDERR: {process.stderr.strip()}", "⚙️")
        else:
            send_log(f"Playwright browser installation command finished with code {process.returncode}.", "⚠️")
            send_log(f"Playwright install STDOUT: {process.stdout.strip()}", "⚙️")
            send_log(f"Playwright install STDERR: {process.stderr.strip()}", "⚙️")
            # Not raising an error here to allow the agent to attempt to start,
            # but logging a significant warning. Tool usage will likely fail.
            send_log(f"{RED}✗ Playwright browser installation may have failed. The agent might not function correctly.{NC}", "❌")
    except FileNotFoundError:
        send_log(f"{RED}✗ Playwright command not found. Ensure Playwright is installed in the environment.{NC}", "❌")
        raise Exception("Playwright command not found. Cannot ensure browser installation.")
    except subprocess.TimeoutExpired:
        send_log(f"{RED}✗ Playwright browser installation timed out after 5 minutes.{NC}", "❌")
        raise Exception("Playwright browser installation timed out.")
    except Exception as e:
        send_log(f"{RED}✗ Error during Playwright browser setup: {e}{NC}", "❌")
        raise # Re-raise critical errors

def _configure_cursor_mcp_json(agent_project_path: Path, api_key=None):
    """Attempts to automatically configure Cursor's mcp.json file."""
    cursor_mcp_file = Path.home() / ".cursor" / "mcp.json"
    server_name = "web-eval-agent-operative"
    
    send_log(f"{BOLD}Attempting to configure Cursor MCP server at {cursor_mcp_file}...{NC}", "⚙️")

    mcp_config = {"mcpServers": {}}
    try:
        if cursor_mcp_file.exists():
            send_log(f"{YELLOW}ℹ Found existing Cursor MCP file: {cursor_mcp_file}{NC}", "🔍")
            with open(cursor_mcp_file, 'r') as f:
                try:
                    mcp_config = json.load(f)
                    if not isinstance(mcp_config, dict):
                        send_log(f"{YELLOW}ℹ Cursor MCP file is not a valid JSON object. Reinitializing.{NC}", "⚠️")
                        mcp_config = {"mcpServers": {}}
                    if "mcpServers" not in mcp_config or not isinstance(mcp_config.get("mcpServers"), dict):
                        send_log(f"{YELLOW}ℹ 'mcpServers' key missing or invalid in Cursor MCP file. Reinitializing.{NC}", "⚠️")
                        mcp_config["mcpServers"] = {}
                except json.JSONDecodeError:
                    send_log(f"{YELLOW}ℹ Error decoding JSON from {cursor_mcp_file}. Backing up and creating new config.{NC}", "⚠️")
                    try:
                        backup_path = cursor_mcp_file.with_suffix(".json.bak")
                        cursor_mcp_file.rename(backup_path)
                        send_log(f"{BOLD}Backed up corrupted MCP file to {backup_path}{NC}", "🛡️")
                    except OSError as e_backup:
                        send_log(f"{RED}✗ Could not back up corrupted MCP file: {e_backup}{NC}", "❌")
                    mcp_config = {"mcpServers": {}}
        else:
            send_log(f"{BOLD}Cursor MCP file not found. Creating new one at {cursor_mcp_file}{NC}", "📝")
            # Ensure .cursor directory exists
            cursor_mcp_file.parent.mkdir(parents=True, exist_ok=True)

        server_config = {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/nandatheguntupalli/web-eval-agent.git",
                "webEvalAgent"
            ],
            "env": {}
        }
        
        # Add API key to environment variables if provided
        if api_key:
            server_config["env"]["OPERATIVE_API_KEY"] = api_key
            send_log(f"{GREEN}✓ API key added to Cursor MCP configuration{NC}", "✅")

        # Add or update the server entry
        mcp_config["mcpServers"][server_name] = server_config
        send_log(f"{BOLD}Updating server entry for '{server_name}' in Cursor MCP config.{NC}", "🛠️")

        with open(cursor_mcp_file, 'w') as f:
            json.dump(mcp_config, f, indent=2)
        
        send_log(f"{GREEN}✓ Successfully updated Cursor MCP configuration at {cursor_mcp_file}.{NC}", "✅")
        send_log(f"{RED}{BOLD}⚠️ IMPORTANT: Please restart Cursor for these changes to take effect!{NC}", "⚠️")
        
        return cursor_mcp_file, mcp_config

    except OSError as e_os:
        send_log(f"{RED}✗ OS Error updating Cursor MCP file {cursor_mcp_file}: {e_os}{NC}", "❌")
        send_log(f"{YELLOW}ℹ Cursor MCP auto-configuration failed. Please configure manually.{NC}", "ℹ️")
    except Exception as e_general:
        send_log(f"{RED}✗ Unexpected error during Cursor MCP auto-configuration: {e_general}{NC}", "❌")
        send_log(f"{YELLOW}ℹ Cursor MCP auto-configuration failed. Please configure manually.{NC}", "ℹ️")
    
    return None, None

def _validate_api_key_server_side(api_key_to_validate):
    """Validates API key with the backend server."""
    send_log(f"{BOLD}Validating API key with Operative servers...{NC}", "➡️")
    try:
        response = requests.get(
            "https://operative-backend.onrender.com/api/validate-key",
            headers={"x-operative-api-key": api_key_to_validate},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if data.get("valid"):
            send_log(f"{GREEN}✓ API key validated successfully server-side.{NC}", "✅")
            return True, data.get("message", "Valid")
        else:
            error_message = data.get("message", "Unknown validation error.")
            send_log(f"{RED}✗ API key validation failed: {error_message}{NC}", "❌")
            return False, error_message
    except requests.exceptions.Timeout:
        send_log(f"{RED}✗ API key validation timed out.{NC}", "❌")
        return False, "Connection to validation server timed out."
    except requests.exceptions.RequestException as e:
        send_log(f"{RED}✗ Could not connect to validation server: {e}{NC}", "❌")
        return False, f"Could not connect to validation server: {e}"
    except json.JSONDecodeError:
        send_log(f"{RED}✗ Invalid JSON response from validation server.{NC}", "❌")
        return False, "Invalid JSON response from validation server."

def get_and_validate_api_key():
    """Gets API key from env, MCP config, or prompts user, then validates and stores it."""
    api_key = os.environ.get("OPERATIVE_API_KEY")
    source = "environment variable"

    # Check MCP config first if environment variable is not set
    if not api_key:
        send_log(f"{YELLOW}ℹ API key not found in environment variables. Checking MCP config...{NC}", "🔍")
        cursor_mcp_file = Path.home() / ".cursor" / "mcp.json"
        server_name = "web-eval-agent-operative"
        
        if cursor_mcp_file.exists():
            try:
                with open(cursor_mcp_file, 'r') as f:
                    mcp_config = json.load(f)
                    if (isinstance(mcp_config, dict) and 
                        "mcpServers" in mcp_config and 
                        isinstance(mcp_config["mcpServers"], dict) and
                        server_name in mcp_config["mcpServers"] and
                        "env" in mcp_config["mcpServers"][server_name] and
                        "OPERATIVE_API_KEY" in mcp_config["mcpServers"][server_name]["env"]):
                        api_key = mcp_config["mcpServers"][server_name]["env"]["OPERATIVE_API_KEY"]
                        source = "MCP config file"
            except (json.JSONDecodeError, OSError):
                send_log(f"{YELLOW}ℹ Error reading Cursor MCP config file. Cannot retrieve API key.{NC}", "⚠️")
    
    # Check legacy config file if MCP config doesn't have the key
    if not api_key:
        send_log(f"{YELLOW}ℹ API key not found in MCP config. Checking legacy config...{NC}", "🔍")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                config_data = json.loads(CONFIG_FILE.read_text())
                api_key = config_data.get("OPERATIVE_API_KEY")
                source = "legacy config file"
            except json.JSONDecodeError:
                send_log(f"{YELLOW}ℹ Error reading legacy API key config file ({CONFIG_FILE}). File might be corrupted.{NC}", "⚠️")
                api_key = None # Ensure api_key is None if file is corrupt
        
    if api_key:
        send_log(f"{YELLOW}ℹ API key found in {source}.{NC}", "📝")
        is_valid, msg = _validate_api_key_server_side(api_key)
        if is_valid:
            OPERATIVE_API_KEY_HOLDER["key"] = api_key
            return api_key
        else:
            send_log(f"{YELLOW}ℹ Invalid API key from {source}: {msg}. Will prompt user.{NC}", "⚠️")
            api_key = None # Reset api_key to trigger prompt

    # Prompt user if no valid key found yet
    while True:
        send_log(f"{BOLD}An Operative.sh API key is required for this installation.{NC}", "🔑")
        send_log(f"If you don't have one, please visit {BOLD}https://operative.sh{NC} to get your key.", "🔑")
        try:
            prompted_key = input("Please enter your Operative.sh API key: ")
        except EOFError: # Happens if stdin is not available (e.g. background process)
             send_log(f"{RED}✗ Could not read API key from input (EOFError). Ensure OPERATIVE_API_KEY is set in the environment or MCP config.{NC}", "❌")
             raise ValueError("API Key could not be obtained via input. Configure it via environment or MCP config.")

        if not prompted_key:
            send_log(f"{RED}✗ API key cannot be empty. Please try again.{NC}", "❌")
            continue
        
        is_valid, msg = _validate_api_key_server_side(prompted_key)
        if is_valid:
            # Save API key to MCP config
            cursor_mcp_file = Path.home() / ".cursor" / "mcp.json"
            server_name = "web-eval-agent-operative"
            try:
                # Update MCP config with API key
                mcp_file, mcp_config = _configure_cursor_mcp_json(Path(".").resolve(), prompted_key)
                if mcp_file:
                    send_log(f"{GREEN}✓ API key validated and saved to MCP config at {mcp_file}{NC}", "✅")
                else:
                    # Fallback to legacy config if MCP config update fails
                    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                    CONFIG_FILE.write_text(json.dumps({"OPERATIVE_API_KEY": prompted_key}))
                    send_log(f"{YELLOW}ℹ Could not save API key to MCP config. Saved to legacy config at {CONFIG_FILE} instead.{NC}", "⚠️")
            except Exception as e:
                send_log(f"{YELLOW}ℹ Could not save API key to MCP config: {e}. Key will not be persisted.{NC}", "⚠️")
            
            OPERATIVE_API_KEY_HOLDER["key"] = prompted_key
            return prompted_key
        else:
            send_log(f"{RED}✗ Validation failed for entered API key: {msg}{NC}", "❌")
            # Provide a way to exit if user doesn't want to retry
            retry = input("Invalid API key. Would you like to try again? (y/n): ")
            if retry.lower() != 'y':
                send_log(f"{RED}✗ API key validation failed. Exiting.{NC}", "❌")
                raise ValueError("Invalid API key and user chose not to retry.")

# --- End of new/modified functions ---

# Get API key from environment variable
# This initial check is now handled by get_and_validate_api_key() in main()
# api_key = os.environ.get('OPERATIVE_API_KEY') 

# Validate the API key
# This initial validation is now handled by get_and_validate_api_key() in main()
# if api_key:
#     is_valid = asyncio.run(validate_api_key_original(api_key)) # Use renamed original
#     if not is_valid:
#         print("Error: Invalid API key. Please provide a valid OperativeAI API key in the OPERATIVE_API_KEY environment variable.")
# else:
#     print("Error: No API key provided. Please set the OPERATIVE_API_KEY environment variable.")


@mcp.tool(name=BrowserTools.WEB_EVAL_AGENT)
async def web_eval_agent(url: str, task: str, ctx: Context, headless_browser: bool = False) -> list[TextContent]:
    """Evaluate the user experience / interface of a web application.

    This tool allows the AI to assess the quality of user experience and interface design
    of a web application by performing specific tasks and analyzing the interaction flow.

    Before this tool is used, the web application should already be running locally on a port.

    Args:
        url: Required. The localhost URL of the web application to evaluate, including the port number.
            Example: http://localhost:3000, http://localhost:8080, http://localhost:4200, http://localhost:5173, etc.
            Try to avoid using the path segments of the URL, and instead use the root URL.
        task: Required. The specific UX/UI aspect to test (e.g., "test the checkout flow",
             "evaluate the navigation menu usability", "check form validation feedback")
             Be as detailed as possible in your task description. It could be anywhere from 2 sentences to 2 paragraphs.
        headless_browser: Optional. Whether to hide the browser window popup during evaluation.
        If headless_browser is True, only the operative control center browser will show, and no popup browser will be shown.

    Returns:
        list[list[TextContent, ImageContent]]: A detailed evaluation of the web application's UX/UI, including
                         observations, issues found, and recommendations for improvement
                         and screenshots of the web application during the evaluation
    """
    headless = headless_browser
    is_valid = await validate_api_key(api_key)

    if not is_valid:
        error_message_str = f"❌ Error: API Key validation failed when running the tool.\\n"
        error_message_str += f"   Reason: {msg}\\n" # Use message from validation
        error_message_str += "   👉 Please check your API key or subscribe at https://operative.sh if it's a limit issue."
        return [TextContent(type="text", text=error_message_str)]
    try:
        # Generate a new tool_call_id for this specific tool call
        tool_call_id = str(uuid.uuid4())
        return await handle_web_evaluation(
            {"url": url, "task": task, "headless": headless, "tool_call_id": tool_call_id},
            ctx,
            current_api_key # Pass the validated key
        )
    except Exception as e:
        tb = traceback.format_exc()
        send_log(f"{RED}✗ Error executing web_eval_agent: {str(e)}\\nTraceback:\\n{tb}{NC}", "❌") # Using send_log
        return [TextContent(
            type="text",
            text=f"Error executing web_eval_agent: {str(e)}\\n\\nTraceback:\\n{tb}"
        )]

# if __name__ == "__main__": # Keep this for direct testing if needed, but ensure API key is handled.
#     try:
#         # Ensure setup for direct run
#         ensure_playwright_browsers()
#         OPERATIVE_API_KEY_HOLDER["key"] = get_and_validate_api_key() # Ensures key for direct run
        
#         send_log(f"Running test evaluation with key: {OPERATIVE_API_KEY_HOLDER['key']}", "🧪")

#         async def run_test_eval():
#             # Need a dummy Context object for direct testing
#             class DummyContext:
#                 async def send_chunk(self, content):
#                     send_log(f"DummyContext chunk: {content}", "📦")
#                 async def send_message(self, message_type, content):
#                     send_log(f"DummyContext message ({message_type}): {content}", "📦")
            
#             await web_eval_agent(
#                 url="http://localhost:5173", 
#                 task="general eval", 
#                 working_directory=".", 
#                 ctx=DummyContext() # Pass a dummy context
#             )
        
#         asyncio.run(run_test_eval())
#     except ValueError as e: # Catch API key validation errors from get_and_validate_api_key
#         send_log(f"Setup for direct run failed: {e}", "❌")
#     except Exception as e:
#         send_log(f"Error during direct test run: {e}\n{traceback.format_exc()}", "❌")
#     finally:
#         # Ensure resources are cleaned up
#         # asyncio.run(cleanup_resources()) # Cleanup now handled in browser_utils
#         send_log("Direct test run finished.", "🏁")

@mcp.tool(name=BrowserTools.SETUP_BROWSER_STATE)
async def setup_browser_state(url: str = None, ctx: Context = None) -> list[TextContent]:
    """Sets up and saves browser state for future use.

    This tool should only be called in one scenario:
    1. The user explicitly requests to set up browser state/authentication

    Launches a non-headless browser for user interaction, allows login/authentication,
    and saves the browser state (cookies, local storage, etc.) to a local file.

    Args:
        url: Optional URL to navigate to upon opening the browser.
        ctx: The MCP context (used for progress reporting, not directly here).

    Returns:
        list[TextContent]: Confirmation of state saving or error messages.
    """
    is_valid = await validate_api_key(api_key)

    if not is_valid:
        error_message_str = "❌ Error: API Key validation failed when running the tool.\n"
        error_message_str += "   Reason: Free tier limit reached.\n"
        error_message_str += "   👉 Please subscribe at https://operative.sh to continue."
        return [TextContent(type="text", text=error_message_str)]
    try:
        # Generate a new tool_call_id for this specific tool call
        tool_call_id = str(uuid.uuid4())
        send_log(f"Generated new tool_call_id for setup_browser_state: {tool_call_id}")
        return await handle_setup_browser_state(
            {"url": url, "tool_call_id": tool_call_id},
            ctx,
            api_key
        )
    except Exception as e:
        tb = traceback.format_exc()
        return [TextContent(
            type="text",
            text=f"Error executing setup_browser_state: {str(e)}\n\nTraceback:\n{tb}"
        )]

def main():
    # Print the ASCII logo
    print(OPERATIVE_LOGO)
    print(f"\n{BLUE}{BOLD}=== 🚀 Welcome to the Operative Web Eval Agent ===\n{NC}")
    
    try:
        send_log(f"{BOLD}Operative Web Eval Agent starting up...{NC}", "🚀")
        
        # 0. Determine agent's project path for MCP configuration
        # Assuming mcp_server.py is in webEvalAgent/ directory, and project root is parent of that.
        # Path(__file__).resolve() gives path to mcp_server.py
        # .parent gives webEvalAgent/
        # .parent again gives the project root.
        try:
            agent_project_path = Path(__file__).resolve().parent.parent
        except NameError: # __file__ not defined (e.g. in interactive interpreter or frozen app)
            # Fallback to current working directory, might not always be correct if script is run from elsewhere
            agent_project_path = Path(".").resolve()
            send_log(f"{YELLOW}ℹ Could not determine agent script path via __file__, falling back to CWD: {agent_project_path}{NC}", "⚠️")

        # Check if we need to configure MCP or if we're running in server mode
        cursor_mcp_file = Path.home() / ".cursor" / "mcp.json"
        server_name = "web-eval-agent-operative"
        is_already_configured = False
        
        if cursor_mcp_file.exists():
            try:
                with open(cursor_mcp_file, 'r') as f:
                    mcp_config = json.load(f)
                    if (isinstance(mcp_config, dict) and 
                        "mcpServers" in mcp_config and 
                        isinstance(mcp_config["mcpServers"], dict) and
                        server_name in mcp_config["mcpServers"]):
                        is_already_configured = True
                        send_log(f"{YELLOW}ℹ Found existing Cursor MCP configuration for {server_name}{NC}", "🔍")
            except (json.JSONDecodeError, OSError):
                # If there's an error reading the file, we'll proceed with configuration
                pass
                
        # First get and validate API key regardless of configuration state
        print(f"\n{BLUE}{BOLD}=== API Key Configuration ==={NC}\n")
        send_log(f"{BOLD}Obtaining and validating API key...{NC}", "🔑")
        operative_key = get_and_validate_api_key()
        if not operative_key:
            send_log(f"{RED}✗ Critical: Operative API key could not be obtained or validated. Exiting.{NC}", "❌")
            return # Exit if no key
            
        send_log(f"{BOLD}Using Operative API Key ending with ...{operative_key[-4:] if len(operative_key) > 4 else operative_key}{NC}", "🔑")
        
        # If not already configured, set up the MCP configuration with the API key
        if not is_already_configured:
            print(f"\n{BLUE}{BOLD}=== Setting up configuration ==={NC}\n")
            # Configure MCP with API key
            _configure_cursor_mcp_json(agent_project_path, operative_key)

            # Ensure Playwright browsers are installed
            print(f"\n{BLUE}{BOLD}=== Checking dependencies ==={NC}\n")
            send_log(f"{BOLD}Checking for Playwright browser installation...{NC}", "🔍")
            ensure_playwright_browsers()
            
            # Installation complete; instruct user to restart and exit
            print(f"\n{BLUE}{BOLD}=== Installation Complete! 🎉 ==={NC}\n")
            send_log(f"{RED}{BOLD}⚠️ IMPORTANT: Please restart Cursor for these changes to take effect!{NC}", "⚠️")
            return
        else:
            # We're running in server mode - update MCP config with API key if needed
            # This updates the key if it's changed since last run
            cursor_mcp_file, _ = _configure_cursor_mcp_json(agent_project_path, operative_key)
            
            send_log(f"{BOLD}API key validated. Starting MCP server...{NC}", "🛰️")
            # Run the FastMCP server
            mcp.run(transport='stdio')

    except ValueError as e: # Catch API key validation errors from get_and_validate_api_key
        send_log(f"{RED}✗ Agent startup failed due to API key issue: {e}{NC}", "❌")
    except Exception as e:
        send_log(f"{RED}✗ An unexpected error occurred during agent startup: {e}\n{traceback.format_exc()}{NC}", "❌")
    finally:
        send_log(f"{BOLD}Operative Web Eval Agent shutting down.{NC}", "🏁")
        send_log(f"{BOLD}Built with ❤️ by Operative.sh{NC}", "📝")
        # Ensure resources are cleaned up
        # asyncio.run(cleanup_resources()) # Cleanup now handled in browser_utils

if __name__ == "__main__":
    main() # Call the main function which now includes setup.
