"""
Low-Level Server for mcp-openapi-proxy.

This server dynamically registers functions (tools) based on an OpenAPI specification,
directly utilizing the spec for tool definitions and invocation.
Configuration is controlled via environment variables:
- OPENAPI_SPEC_URL: URL to the OpenAPI specification.
- TOOL_WHITELIST: Comma-separated list of allowed endpoint paths.
- SERVER_URL_OVERRIDE: Optional override for the base URL from the OpenAPI spec.
- API_KEY: Generic token for Bearer header.
- STRIP_PARAM: Param name (e.g., "auth") to remove from parameters.
- EXTRA_HEADERS: Additional headers in 'Header: Value' format, one per line.
"""

import os
import sys
import asyncio
import json
import requests
from typing import List, Dict, Any
from pydantic import BaseModel
from mcp import types
from urllib.parse import unquote
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp_openapi_proxy.utils import (
    setup_logging,
    normalize_tool_name,
    is_tool_whitelisted,
    fetch_openapi_spec,
    build_base_url,
    handle_auth,
    strip_parameters,
    detect_response_type,
    get_additional_headers
)

DEBUG = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
logger = setup_logging(debug=DEBUG)

# Define a simple pydantic model to ensure "root" field exists
class SimpleServerResult(BaseModel):
    root: types.CallToolResult

tools: List[types.Tool] = []
resources: List[types.Resource] = [
    types.Resource(
        name="spec_file",
        uri="file:///openapi_spec.json",
        description="The raw OpenAPI specification JSON"
    )
]
prompts: List[types.Prompt] = [
    types.Prompt(
        name="summarize_spec",
        description="Summarizes the purpose of the OpenAPI specification",
        arguments=[],
        messages=lambda args: [
            {"role": "assistant", "content": {"type": "text", "text": "This OpenAPI spec defines an API’s endpoints, parameters, and responses, making it a blueprint for devs to build and integrate stuff without errors."}}
        ]
    )
]
openapi_spec_data = None

mcp = Server("OpenApiProxy-LowLevel")

async def dispatcher_handler(request: types.CallToolRequest) -> SimpleServerResult:
    """Dispatcher handler that routes CallToolRequest to the appropriate function (tool)."""
    global openapi_spec_data
    try:
        function_name = request.params.name
        logger.debug(f"Dispatcher received CallToolRequest for function: {function_name}")
        logger.debug(f"API_KEY: {os.getenv('API_KEY', '<not set>')[:5] + '...' if os.getenv('API_KEY') else '<not set>'}")
        logger.debug(f"STRIP_PARAM: {os.getenv('STRIP_PARAM', '<not set>')}")
        tool = next((tool for tool in tools if tool.name == function_name), None)
        if not tool:
            logger.error(f"Unknown function requested: {function_name}")
            return SimpleServerResult(root=types.CallToolResult(
                content=[types.TextContent(type="text", text="Unknown function requested")]
            ))
        arguments = request.params.arguments or {}
        logger.debug(f"Raw arguments before processing: {arguments}")

        operation_details = lookup_operation_details(function_name, openapi_spec_data)
        if not operation_details:
            logger.error(f"Could not find OpenAPI operation for function: {function_name}")
            return SimpleServerResult(root=types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Could not find OpenAPI operation for function: {function_name}")]
            ))

        operation = operation_details['operation']
        operation['method'] = operation_details['method']
        headers = handle_auth(operation)
        additional_headers = get_additional_headers()
        headers = {**headers, **additional_headers}
        parameters = dict(strip_parameters(arguments))
        method = operation_details['method']
        if method != "GET":
            headers["Content-Type"] = "application/json"

        path = operation_details['path']
        try:
            path = path.format(**parameters)
            logger.debug(f"Substituted path using format(): {path}")
            if method == "GET":
                placeholder_keys = [seg.strip('{}') for seg in operation_details['original_path'].split('/') if seg.startswith('{') and seg.endswith('}')]
                for key in placeholder_keys:
                    parameters.pop(key, None)
        except KeyError as e:
            logger.error(f"Missing parameter for substitution: {e}")
            return SimpleServerResult(root=types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Missing parameter: {e}")]
            ).dict())

        base_url = build_base_url(openapi_spec_data)
        if not base_url:
            logger.critical("Failed to construct base URL from spec or SERVER_URL_OVERRIDE.")
            return SimpleServerResult(root=types.CallToolResult(
                content=[types.TextContent(type="text", text="No base URL defined in spec or SERVER_URL_OVERRIDE")]
            ).dict())

        api_url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        request_params = {}
        request_body = None
        if isinstance(parameters, dict):
            merged_params = []
            path_item = openapi_spec_data.get("paths", {}).get(operation_details['original_path'], {})
            if isinstance(path_item, dict) and "parameters" in path_item:
                merged_params.extend(path_item["parameters"])
            if "parameters" in operation:
                merged_params.extend(operation["parameters"])
            path_params_in_openapi = [param["name"] for param in merged_params if param.get("in") == "path"]
            if path_params_in_openapi:
                missing_required = [
                    param["name"] for param in merged_params
                    if param.get("in") == "path" and param.get("required", False) and param["name"] not in arguments
                ]
                if missing_required:
                    logger.error(f"Missing required path parameters: {missing_required}")
                    return SimpleServerResult(root=types.CallToolResult(
                        content=[types.TextContent(type="text", text=f"Missing required path parameters: {missing_required}")]
                    ).dict())
            if method == "GET":
                request_params = parameters
            else:
                request_body = parameters
        else:
            logger.debug("No valid parameters provided, proceeding without params/body")

        logger.debug(f"API Request - URL: {api_url}, Method: {method}")
        logger.debug(f"Headers: {headers}")
        logger.debug(f"Query Params: {request_params}")
        logger.debug(f"Request Body: {request_body}")

        try:
            response = requests.request(
                method=method,
                url=api_url,
                headers=headers,
                params=request_params if method == "GET" else None,
                json=request_body if method != "GET" else None
            )
            response.raise_for_status()
            response_text = (response.text or "No response body").strip()
            content, log_message = detect_response_type(response_text)
            logger.debug(log_message)
            final_content = [content]
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            return SimpleServerResult(root=types.CallToolResult(
                content=[types.TextContent(type="text", text=str(e))]
            ).dict())
        logger.debug(f"Response content type: {content.type}")
        logger.debug(f"Response sent to client: {content.text}")
        validated_content = [c.model_dump() if hasattr(c, "model_dump") else c for c in final_content]
        logger.debug(f"Final content array: {validated_content}")
        return SimpleServerResult(root=types.CallToolResult(content=validated_content))
    except Exception as e:
        logger.error(f"Unhandled exception in dispatcher_handler: {e}", exc_info=True)
        return SimpleServerResult(root=types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Internal error: {str(e)}")]
        ).dict())

def lookup_operation_details(function_name: str, spec: Dict) -> Dict or None:
    if not spec or 'paths' not in spec:
        return None
    for path, path_item in spec['paths'].items():
        for method, operation in path_item.items():
            if method.lower() not in ['get', 'post', 'put', 'delete', 'patch']:
                continue
            raw_name = f"{method.upper()} {path}"
            current_function_name = normalize_tool_name(raw_name)
            if current_function_name == function_name:
                return {"path": path, "method": method.upper(), "operation": operation, "original_path": path}
    return None

async def list_tools(request: types.ListToolsRequest) -> types.ServerResult:
    logger.debug("Handling list_tools request - start")
    logger.debug(f"Tools list length: {len(tools)}")
    result = types.ListToolsResult(tools=tools)
    return types.ServerResult(root=result)

async def list_resources(request: types.ListResourcesRequest) -> types.ServerResult:
    logger.debug("Handling list_resources request")
    logger.debug(f"Resources list length: {len(resources)}")
    result = types.ListResourcesResult(resources=resources, resourceTemplates=[])
    return types.ServerResult(root=result)

async def read_resource(request: types.ReadResourceRequest) -> types.ServerResult:
    logger.debug(f"Handling read_resource request for {request.params.uri}")
    global openapi_spec_data
    resource = next((r for r in resources if r.uri == request.params.uri), None)
    if not resource or request.params.uri != "file:///openapi_spec.json":
        logger.error(f"Resource '{request.params.uri}' not found")
        result = types.ReadResourceResult(contents=[{"type": "text", "content": "Resource not found"}])
        return types.ServerResult(root=result)
    try:
        if not openapi_spec_data:
            logger.warning("OpenAPI spec data missing, attempting to refetch")
            openapi_url = os.getenv('OPENAPI_SPEC_URL')
            if not openapi_url:
                logger.error("OPENAPI_SPEC_URL not set for refetch")
                result = types.ReadResourceResult(contents=[{"type": "text", "content": "Spec unavailable: OPENAPI_SPEC_URL not set"}])
                return types.ServerResult(root=result)
            openapi_spec_data = fetch_openapi_spec(openapi_url)
            if not openapi_spec_data:
                logger.error("Failed to refetch OpenAPI spec data")
                result = types.ReadResourceResult(contents=[{"type": "text", "content": "Spec data unavailable after refetch attempt"}])
                return types.ServerResult(root=result)
            logger.info("Successfully refetched OpenAPI spec data")
        spec_json = json.dumps(openapi_spec_data, indent=2)
        logger.debug(f"Serving spec JSON: {spec_json[:50]}...")
        result = types.ReadResourceResult(contents=[types.TextContent(type="text", text=spec_json)])
        return types.ServerResult(root=result)
    except Exception as e:
        logger.error(f"Error reading resource: {e}", exc_info=True)
        result = types.ReadResourceResult(contents=[types.TextContent(type="text", text=f"Resource error: {str(e)}")])
        return types.ServerResult(root=result)

async def list_prompts(request: types.ListPromptsRequest) -> types.ServerResult:
    logger.debug("Handling list_prompts request")
    logger.debug(f"Prompts list length: {len(prompts)}")
    result = types.ListPromptsResult(prompts=prompts)
    return types.ServerResult(root=result)

async def get_prompt(request: types.GetPromptRequest) -> types.ServerResult:
    logger.debug(f"Handling get_prompt request for {request.params.name}")
    prompt = next((p for p in prompts if p.name == request.params.name), None)
    if not prompt:
        logger.error(f"Prompt '{request.params.name}' not found")
        result = types.GetPromptResult(messages=[{"role": "system", "content": {"type": "text", "text": "Prompt not found"}}])
        return types.ServerResult(root=result)
    try:
        messages = prompt.messages(request.params.arguments or {})
        logger.debug(f"Generated messages: {messages}")
        result = types.GetPromptResult(messages=messages)
        return types.ServerResult(root=result)
    except Exception as e:
        logger.error(f"Error generating prompt: {e}", exc_info=True)
        result = types.GetPromptResult(messages=[{"role": "system", "content": {"type": "text", "text": f"Prompt error: {str(e)}"}}])
        return types.ServerResult(root=result)

def register_functions(spec: Dict) -> List[types.Tool]:
    """Register tools from OpenAPI spec, preserving across calls if already populated."""
    global tools
    logger.debug("Clearing previously registered tools to allow re-registration")
    tools.clear()
    if not spec:
        logger.error("OpenAPI spec is None or empty.")
        return tools
    if 'paths' not in spec:
        logger.error("No 'paths' key in OpenAPI spec.")
        return tools
    logger.debug(f"Spec paths available: {list(spec['paths'].keys())}")
    filtered_paths = {path: item for path, item in spec['paths'].items() if is_tool_whitelisted(path)}
    logger.debug(f"Filtered paths: {list(filtered_paths.keys())}")
    if not filtered_paths:
        logger.warning("No whitelisted paths found in OpenAPI spec after filtering.")
        return tools
    for path, path_item in filtered_paths.items():
        if not path_item:
            logger.debug(f"Empty path item for {path}")
            continue
        for method, operation in path_item.items():
            if method.lower() not in ['get', 'post', 'put', 'delete', 'patch']:
                logger.debug(f"Skipping unsupported method {method} for {path}")
                continue
            try:
                raw_name = f"{method.upper()} {path}"
                function_name = normalize_tool_name(raw_name)
                description = operation.get('summary', operation.get('description', 'No description available'))
                input_schema = {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
                parameters = operation.get('parameters', [])
                placeholder_params = [part.strip('{}') for part in path.split('/') if '{' in part and '}' in part]
                for param_name in placeholder_params:
                    input_schema['properties'][param_name] = {
                        "type": "string",
                        "description": f"Path parameter {param_name}"
                    }
                    input_schema['required'].append(param_name)
                    logger.debug(f"Added URI placeholder {param_name} to inputSchema for {function_name}")
                for param in parameters:
                    param_name = param.get('name')
                    param_in = param.get('in')
                    if param_in in ['path', 'query']:
                        param_type = param.get('schema', {}).get('type', 'string')
                        schema_type = param_type if param_type in ['string', 'integer', 'boolean', 'number'] else 'string'
                        input_schema['properties'][param_name] = {
                            "type": schema_type,
                            "description": param.get('description', f"{param_in} parameter {param_name}")
                        }
                        if param.get('required', False) and param_name not in input_schema['required']:
                            input_schema['required'].append(param_name)
                tool = types.Tool(
                    name=function_name,
                    description=description,
                    inputSchema=input_schema,
                )
                tools.append(tool)
                logger.debug(f"Registered function: {function_name} ({method.upper()} {path}) with inputSchema: {json.dumps(input_schema)}")
            except Exception as e:
                logger.error(f"Error registering function for {method.upper()} {path}: {e}", exc_info=True)
    logger.info(f"Registered {len(tools)} functions from OpenAPI spec.")
    return tools

def lookup_operation_details(function_name: str, spec: Dict) -> Dict or None:
    if not spec or 'paths' not in spec:
        return None
    for path, path_item in spec['paths'].items():
        for method, operation in path_item.items():
            if method.lower() not in ['get', 'post', 'put', 'delete', 'patch']:
                continue
            raw_name = f"{method.upper()} {path}"
            current_function_name = normalize_tool_name(raw_name)
            if current_function_name == function_name:
                return {"path": path, "method": method.upper(), "operation": operation, "original_path": path}
    return None

async def start_server():
    logger.debug("Starting Low-Level MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            initialization_options=InitializationOptions(
                server_name="AnyOpenAPIMCP-LowLevel",
                server_version="0.1.0",
                capabilities=types.ServerCapabilities(
                    tools=types.ToolsCapability(listChanged=True),
                    prompts=types.PromptsCapability(listChanged=True),
                    resources=types.ResourcesCapability(listChanged=True)
                ),
            ),
        )

def run_server():
    global openapi_spec_data
    try:
        openapi_url = os.getenv('OPENAPI_SPEC_URL')
        if not openapi_url:
            logger.critical("OPENAPI_SPEC_URL environment variable is required but not set.")
            sys.exit(1)
        openapi_spec_data = fetch_openapi_spec(openapi_url)
        if not openapi_spec_data:
            logger.critical("Failed to fetch or parse OpenAPI specification from OPENAPI_SPEC_URL.")
            sys.exit(1)
        logger.info("OpenAPI specification fetched successfully.")
        register_functions(openapi_spec_data)
        logger.debug(f"Tools after registration: {[tool.name for tool in tools]}")
        if not tools:
            logger.critical("No valid tools registered. Shutting down.")
            sys.exit(1)
        mcp.request_handlers[types.ListToolsRequest] = list_tools
        mcp.request_handlers[types.CallToolRequest] = dispatcher_handler
        mcp.request_handlers[types.ListResourcesRequest] = list_resources
        mcp.request_handlers[types.ReadResourceRequest] = read_resource
        mcp.request_handlers[types.ListPromptsRequest] = list_prompts
        mcp.request_handlers[types.GetPromptRequest] = get_prompt
        logger.debug("Handlers registered.")
        asyncio.run(start_server())
    except KeyboardInterrupt:
        logger.debug("MCP server shutdown initiated by user.")
    except Exception as e:
        logger.critical(f"Failed to start MCP server: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    run_server()
