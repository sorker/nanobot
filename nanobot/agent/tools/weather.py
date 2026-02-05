"""Weather query tool using free APIs."""

import urllib.parse
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool


class WeatherTool(Tool):
    """
    Tool to query weather information.
    
    Uses free wttr.in API that doesn't require API keys.
    Supports city names, airport codes, and coordinates.
    """
    
    @property
    def name(self) -> str:
        return "weather"
    
    @property
    def description(self) -> str:
        return (
            "Get current weather and forecasts for a location. "
            "Uses free wttr.in service that doesn't require API keys. "
            "Supports city names (e.g., 'Shanghai', 'London'), airport codes (e.g., 'JFK'), "
            "and coordinates (e.g., '51.5,-0.12')."
        )
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location name, airport code, or coordinates (lat,lon)"
                },
                "format": {
                    "type": "string",
                    "description": "Output format: 'compact' (one line), 'full' (detailed). Default: 'compact'",
                    "enum": ["compact", "full"]
                },
                "units": {
                    "type": "string",
                    "description": "Temperature units: 'metric' (Celsius) or 'imperial' (Fahrenheit). Default: 'metric'",
                    "enum": ["metric", "imperial"]
                },
                "days": {
                    "type": "integer",
                    "description": "Number of forecast days (0=current only, 1=today, 3=3 days). Default: 1",
                    "enum": [0, 1, 3]
                }
            },
            "required": ["location"]
        }
    
    async def execute(
        self,
        location: str,
        format: str = "compact",
        units: str = "metric",
        days: int = 1,
        **kwargs: Any
    ) -> str:
        if not location.strip():
            return "Error: location parameter is required"
        
        try:
            # URL encode location
            encoded_location = urllib.parse.quote(location.strip())
            
            # Query wttr.in API
            result = await self._query_wttr_in(encoded_location, format, units, days)
            return result
            
        except Exception as e:
            return f"Failed to fetch weather: {str(e)}"
    
    async def _query_wttr_in(
        self,
        location: str,
        format_type: str,
        units: str,
        days: int
    ) -> str:
        """Query wttr.in API."""
        # Build URL
        base_url = f"https://wttr.in/{location}"
        
        # Build query parameters
        params = []
        
        # Format parameter
        if format_type == "compact":
            params.append("format=3")  # Compact format
        elif format_type == "full":
            params.append("T")  # Full format without colors
        else:
            params.append("format=3")  # Default to compact
        
        # Units parameter
        if units == "imperial":
            params.append("u")  # USCS units
        else:
            params.append("m")  # Metric units
        
        # Days parameter
        if days == 0:
            params.append("0")  # Current only
        elif days == 1:
            params.append("1")  # Today
        elif days == 3:
            params.append("3")  # 3 days
        
        # Combine URL
        if params:
            url = f"{base_url}?{'&'.join(params)}"
        else:
            url = base_url
        
        # Send request
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10.0)
                response.raise_for_status()
                
                text = response.text.strip()
                
                if not text:
                    return f"Error: No weather data returned for location: {location}"
                
                # Add helpful context for compact format
                if format_type == "compact":
                    return f"Weather for {location}:\n{text}"
                
                return text
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"Error: Location not found: {location}"
            return f"Error: HTTP {e.response.status_code}"
        except httpx.TimeoutException:
            return "Error: Request timed out. Please try again."
        except Exception as e:
            return f"Error: {str(e)}"
