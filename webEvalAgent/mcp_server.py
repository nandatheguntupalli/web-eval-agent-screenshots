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
# Assuming send_log is available here, adjust if necessary
from webEvalAgent.src.log_server import send_log # Placeholder, ensure this is correct

# Placeholder for send_log if not correctly imported above.
# If you have a central logging setup, use that. Otherwise, this is a basic fallback.
# def send_log(message, emoji=""):
#     print(f"{emoji} {message}")

# Set the API key to a fake key to avoid error in backend
os.environ["ANTHROPIC_API_KEY"] = 'not_a_real_key'
os.environ["ANONYMIZED_TELEMETRY"] = 'false'

# MCP imports
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import TextContent

# Import our modules
from webEvalAgent.src.browser_manager import PlaywrightBrowserManager
# from webEvalAgent.src.browser_utils import cleanup_resources # Removed import
from webEvalAgent.src.api_utils import validate_api_key as validate_api_key_original # Renamed to avoid conflict
from webEvalAgent.src.tool_handlers import handle_web_evaluation

stop_log_server() # Stop the log server before starting the MCP server

# Create the MCP server
mcp = FastMCP("Operative")

# Define the browser tools
class BrowserTools(str, Enum):
    WEB_EVAL_AGENT = "web_eval_agent"

# Parse command line arguments (keeping the parser for potential future arguments)
# parser = argparse.ArgumentParser(description='Run the MCP server with browser debugging capabilities')
# args = parser.parse_args()

# --- Start of new/modified functions ---

CONFIG_DIR = Path.home() / ".operative"
CONFIG_FILE = CONFIG_DIR / "config.json"
OPERATIVE_API_KEY_HOLDER = {"key": None} # Global holder for the validated API key

def ensure_playwright_browsers():
    """Checks and installs Playwright browsers if necessary."""
    try:
        send_log("Checking and installing Playwright browsers if necessary...", "🚀")
        # Using playwright's Python API to check/install is more robust if available.
        # For now, calling the CLI command.
        # Ensure playwright is in the path for uvx environment.
        process = subprocess.run(["playwright", "install", "--with-deps"], capture_output=True, text=True, check=False, timeout=300)
        if process.returncode == 0:
            send_log("Playwright browsers are installed/updated.", "✅")
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
            send_log("Playwright browser installation may have failed. The agent might not function correctly.", "❌")
    except FileNotFoundError:
        send_log("Playwright command not found. Ensure Playwright is installed in the environment.", "❌")
        raise Exception("Playwright command not found. Cannot ensure browser installation.")
    except subprocess.TimeoutExpired:
        send_log("Playwright browser installation timed out after 5 minutes.", "❌")
        raise Exception("Playwright browser installation timed out.")
    except Exception as e:
        send_log(f"Error during Playwright browser setup: {e}", "❌")
        raise # Re-raise critical errors

def _configure_cursor_mcp_json(agent_project_path: Path):
    """Attempts to automatically configure Cursor's mcp.json file."""
    cursor_mcp_file = Path.home() / ".cursor" / "mcp.json"
    server_name = "web-eval-agent-operative"
    
    send_log(f"Attempting to configure Cursor MCP server at {cursor_mcp_file}...", "⚙️")

    mcp_config = {"mcpServers": {}}
    try:
        if cursor_mcp_file.exists():
            send_log(f"Found existing Cursor MCP file: {cursor_mcp_file}", "🔍")
            with open(cursor_mcp_file, 'r') as f:
                try:
                    mcp_config = json.load(f)
                    if not isinstance(mcp_config, dict):
                        send_log("Cursor MCP file is not a valid JSON object. Reinitializing.", "⚠️")
                        mcp_config = {"mcpServers": {}}
                    if "mcpServers" not in mcp_config or not isinstance(mcp_config.get("mcpServers"), dict):
                        send_log("'mcpServers' key missing or invalid in Cursor MCP file. Reinitializing.", "⚠️")
                        mcp_config["mcpServers"] = {}
                except json.JSONDecodeError:
                    send_log(f"Error decoding JSON from {cursor_mcp_file}. Backing up and creating new config.", "⚠️")
                    try:
                        backup_path = cursor_mcp_file.with_suffix(".json.bak")
                        cursor_mcp_file.rename(backup_path)
                        send_log(f"Backed up corrupted MCP file to {backup_path}", "🛡️")
                    except OSError as e_backup:
                        send_log(f"Could not back up corrupted MCP file: {e_backup}", "❌")
                    mcp_config = {"mcpServers": {}}
        else:
            send_log(f"Cursor MCP file not found. Creating new one at {cursor_mcp_file}", "📝")
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
            # API key is handled by the agent itself now, so not explicitly set here.
        }

        # Add or update the server entry
        mcp_config["mcpServers"][server_name] = server_config
        send_log(f"Updating server entry for '{server_name}' in Cursor MCP config.", "🛠️")

        with open(cursor_mcp_file, 'w') as f:
            json.dump(mcp_config, f, indent=2)
        
        send_log(f"Successfully updated Cursor MCP configuration at {cursor_mcp_file}.", "✅")
        send_log("IMPORTANT: Please restart Cursor for these changes to take effect.", "⚠️")

    except OSError as e_os:
        send_log(f"OS Error updating Cursor MCP file {cursor_mcp_file}: {e_os}", "❌")
        send_log("Cursor MCP auto-configuration failed. Please configure manually.", "ℹ️")
    except Exception as e_general:
        send_log(f"Unexpected error during Cursor MCP auto-configuration: {e_general}", "❌")
        send_log("Cursor MCP auto-configuration failed. Please configure manually.", "ℹ️")

def _validate_api_key_server_side(api_key_to_validate):
    """Validates API key with the backend server."""
    send_log("Validating API key with Operative servers...", "➡️")
    try:
        response = requests.get(
            "https://operative-backend.onrender.com/api/validate-key",
            headers={"x-operative-api-key": api_key_to_validate},
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if data.get("valid"):
            send_log("API key validated successfully server-side.", "✅")
            return True, data.get("message", "Valid")
        else:
            error_message = data.get("message", "Unknown validation error.")
            send_log(f"API key validation failed: {error_message}", "❌")
            return False, error_message
    except requests.exceptions.Timeout:
        send_log("API key validation timed out.", "❌")
        return False, "Connection to validation server timed out."
    except requests.exceptions.RequestException as e:
        send_log(f"Could not connect to validation server: {e}", "❌")
        return False, f"Could not connect to validation server: {e}"
    except json.JSONDecodeError:
        send_log("Invalid JSON response from validation server.", "❌")
        return False, "Invalid JSON response from validation server."

def get_and_validate_api_key():
    """Gets API key from env, local config, or prompts user, then validates and stores it."""
    api_key = os.environ.get("OPERATIVE_API_KEY")
    source = "environment variable"

    if not api_key:
        send_log("API key not found in environment variables. Checking local config...", "🔍")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            try:
                config_data = json.loads(CONFIG_FILE.read_text())
                api_key = config_data.get("OPERATIVE_API_KEY")
                source = "local config file"
            except json.JSONDecodeError:
                send_log(f"Error reading local API key config file ({CONFIG_FILE}). File might be corrupted.", "⚠️")
                api_key = None # Ensure api_key is None if file is corrupt
        
    if api_key:
        send_log(f"API key found in {source}.", "📝")
        is_valid, msg = _validate_api_key_server_side(api_key)
        if is_valid:
            OPERATIVE_API_KEY_HOLDER["key"] = api_key
            return api_key
        else:
            send_log(f"Invalid API key from {source}: {msg}. Will prompt user.", "⚠️")
            api_key = None # Reset api_key to trigger prompt

    # Prompt user if no valid key found yet
    while True:
        send_log("Operative API Key is required.", "🔑")
        # This input method might not work reliably when run as a background MCP server.
        # A more robust solution (e.g., a first-run setup UI or command) might be needed for some MCP clients.
        try:
            prompted_key = input("Please enter your Operative.sh API key: ")
        except EOFError: # Happens if stdin is not available (e.g. background process)
             send_log("Could not read API key from input (EOFError). Ensure OPERATIVE_API_KEY is set in the environment or ~/.operative/config.json", "❌")
             raise ValueError("API Key could not be obtained via input. Configure it via environment or local file.")

        if not prompted_key:
            send_log("API key cannot be empty. Please try again.", "❌")
            continue
        
        is_valid, msg = _validate_api_key_server_side(prompted_key)
        if is_valid:
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True) # Ensure dir exists again
                CONFIG_FILE.write_text(json.dumps({"OPERATIVE_API_KEY": prompted_key}))
                send_log(f"API key validated and saved to {CONFIG_FILE}", "✅")
            except IOError as e:
                send_log(f"Could not save API key to {CONFIG_FILE}: {e}. Key will not be persisted.", "⚠️")
            OPERATIVE_API_KEY_HOLDER["key"] = prompted_key
            return prompted_key
        else:
            send_log(f"Validation failed for entered API key: {msg}", "❌")
            # Provide a way to exit if user doesn't want to retry
            retry = input("Invalid API key. Would you like to try again? (y/n): ")
            if retry.lower() != 'y':
                send_log("API key validation failed. Exiting.", "❌")
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
async def web_eval_agent(url: str, task: str, working_directory: str, ctx: Context) -> list[TextContent]:
    """Evaluate the user experience / interface of a web application.

    This tool allows the AI to assess the quality of user experience and interface design
    of a web application by performing specific tasks and analyzing the interaction flow.

    Before this tool is used, the web application should already be running locally in a separate terminal.

    Args:
        url: Required. The localhost URL of the web application to evaluate, including the port number.
        task: Required. The specific UX/UI aspect to test (e.g., "test the checkout flow",
             "evaluate the navigation menu usability", "check form validation feedback")
             Be as detailed as possible in your task description. It could be anywhere from 2 sentences to 2 paragraphs.
        working_directory: Required. The root directory of the project
        external_browser: Optional. Whether to show the browser window externally during evaluation. Defaults to False. 

    Returns:
        list[list[TextContent, ImageContent]]: A detailed evaluation of the web application's UX/UI, including
                         observations, issues found, and recommendations for improvement
                         and screenshots of the web application during the evaluation
    """
    external_browser = True
    # Convert external_browser to headless parameter (inverse logic)
    headless = not external_browser
    
    current_api_key = OPERATIVE_API_KEY_HOLDER.get("key")
    if not current_api_key:
        # This should ideally not happen if main() enforced key validation.
        send_log("API key not available in tool function. This indicates a setup issue.", "❌")
        return [TextContent(type="text", text="❌ Error: Operative API Key is missing or was not validated during startup.")]

    # Re-validate the key or rely on the initial validation? 
    # The original code re-validates here using validate_api_key_original.
    # For simplicity and to trust the startup validation, we can use the stored key.
    # However, if the key could become invalid server-side during a long session, re-validation might be desired.
    # Let's stick to the new server-side validation for consistency if we re-validate.
    
    is_valid, msg = await asyncio.to_thread(_validate_api_key_server_side, current_api_key)

    if not is_valid:
        error_message_str = f"❌ Error: API Key validation failed when running the tool.\\n"
        error_message_str += f"   Reason: {msg}\\n" # Use message from validation
        error_message_str += "   👉 Please check your API key or subscribe at https://operative.sh if it's a limit issue."
        return [TextContent(type="text", text=error_message_str)]
    try:
        # Generate a new tool_call_id for this specific tool call
        tool_call_id = str(uuid.uuid4())
        send_log(f"Generated new tool_call_id for web_eval_agent: {tool_call_id}", "🆔") # Using send_log
        return await handle_web_evaluation(
            {"url": url, "task": task, "headless": headless, "tool_call_id": tool_call_id},
            ctx,
            current_api_key # Pass the validated key
        )
    except Exception as e:
        tb = traceback.format_exc()
        send_log(f"Error executing web_eval_agent: {str(e)}\\nTraceback:\\n{tb}", "❌") # Using send_log
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


def main():
    # Startup initiated via send_log; removed loud print statement
    try:
        send_log("Operative Web Eval Agent starting up...", "🚀")
        
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
            send_log(f"Could not determine agent script path via __file__, falling back to CWD: {agent_project_path}", "⚠️")

        # Attempt to configure Cursor MCP
        _configure_cursor_mcp_json(agent_project_path)

        # 1. Ensure Playwright browsers are installed
        ensure_playwright_browsers()
        
        # 2. Get and validate API key
        # This will prompt if not found in env or local config.
        # The validated key is stored in OPERATIVE_API_KEY_HOLDER["key"]
        operative_key = get_and_validate_api_key()
        if not operative_key: # Should not happen if get_and_validate_api_key raises on failure
            send_log("Critical: Operative API key could not be obtained or validated. Exiting.", "❌")
            return # Exit if no key

        send_log(f"Using Operative API Key ending with ...{operative_key[-4:] if len(operative_key) > 4 else operative_key}", "🔑")
        
        # Run the FastMCP server
        send_log("Starting MCP server...", "🛰️")
        mcp.run(transport='stdio')

    except ValueError as e: # Catch API key validation errors from get_and_validate_api_key
        send_log(f"Agent startup failed due to API key issue: {e}", "❌")
    except Exception as e:
        send_log(f"An unexpected error occurred during agent startup: {e}\n{traceback.format_exc()}", "❌")
    finally:
        send_log("Operative Web Eval Agent shutting down.", "🏁")
        # Ensure resources are cleaned up
        # asyncio.run(cleanup_resources()) # Cleanup now handled in browser_utils

if __name__ == "__main__":
    main() # Call the main function which now includes setup.
