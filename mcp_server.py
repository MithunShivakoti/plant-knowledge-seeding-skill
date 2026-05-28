import httpx
import json
import os
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SEED_API_URL = os.environ.get('SEED_API_URL', 'http://localhost:8000')

server = Server('nymph-plant-seeder')

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name='seed_plant',
            description='Seeds structured plant knowledge for a wastewater treatment plant given its NPDES permit number. Returns permit limits, process train, compliance history, and full audit trail.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'npdes': {
                        'type': 'string',
                        'description': 'NPDES permit number e.g. AL0061671'
                    },
                    'plant': {
                        'type': 'string',
                        'description': 'Plant name e.g. Eufaula WWTP'
                    },
                    'location': {
                        'type': 'string',
                        'description': 'City and state e.g. Eufaula, Alabama'
                    }
                },
                'required': ['npdes']
            }
        ),
        Tool(
            name='list_seeded_plants',
            description='Lists all wastewater treatment plants that have already been seeded and are available instantly.',
            inputSchema={
                'type': 'object',
                'properties': {}
            }
        ),
        Tool(
            name='get_plant_summary',
            description='Gets a formatted summary of a seeded plant including process type, design flow, permit limits count, and compliance status.',
            inputSchema={
                'type': 'object',
                'properties': {
                    'npdes': {
                        'type': 'string',
                        'description': 'NPDES permit number'
                    }
                },
                'required': ['npdes']
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    async with httpx.AsyncClient(timeout=600) as client:

        if name == 'list_seeded_plants':
            resp = await client.get(f'{SEED_API_URL}/plants')
            plants = resp.json().get('plants', [])
            lines = ['Seeded plants available:']
            for p in plants:
                lines.append(f'- {p["npdes"]}: {p["plantName"]} ({p["city"]}, {p["state"]})')
            return [TextContent(type='text', text='\n'.join(lines))]

        elif name == 'seed_plant':
            npdes = arguments.get('npdes', '').upper().strip()
            plant = arguments.get('plant', f'Plant {npdes}')
            location = arguments.get('location', 'Alabama')

            resp = await client.post(f'{SEED_API_URL}/seed', json={
                'plant': plant,
                'location': location,
                'npdes': npdes
            })

            if resp.status_code != 200:
                return [TextContent(type='text', text=f'Error: {resp.text}')]

            data = resp.json()
            seed = data.get('data', {})
            identity = seed.get('identity', {})
            basics = seed.get('overview', {}).get('basics', {})
            limits = seed.get('permitLimits', [])
            logs = seed.get('nymphLogs', {})
            cached = data.get('cached', False)

            summary = f"""Plant seeded: {identity.get('plantName')} ({npdes})
{'[from cache]' if cached else '[freshly seeded]'}

Identity:
- City: {identity.get('city')}, {identity.get('state')} {identity.get('zip')}
- Owner: {identity.get('owner')}
- Licensed: {seed.get('identityPatch', {}).get('licensed')}

Overview:
- Process Type: {basics.get('processType')}
- Influent Type: {basics.get('influentType')}
- Design Flow: {basics.get('designFlowMGD')} MGD

Permit Limits: {len(limits)} parameters
Process Train: {len(seed.get('overview', {}).get('processTrain', {}).get('liquid', []))} liquid nodes

Nymph Logs:
- Operating History: {len(logs.get('operatingHistory', []))} entries
- Historical Outcomes: {len(logs.get('historicalOutcomes', []))} entries
- Procedures: {len(logs.get('proceduresPreferences', []))} entries
- Chemicals: {len(logs.get('chemicalsDosing', []))} entries

Audit:
- Sources searched: {len(seed.get('runAudit', {}).get('sourcesSearched', []))}
- Facts skipped: {len(seed.get('runAudit', {}).get('factsSkipped', []))}
- Conflicts: {len(seed.get('runAudit', {}).get('conflicts', []))}

Full seed package available at: {SEED_API_URL}/seed/{npdes}"""

            return [TextContent(type='text', text=summary)]

        elif name == 'get_plant_summary':
            npdes = arguments.get('npdes', '').upper().strip()
            resp = await client.get(f'{SEED_API_URL}/seed/{npdes}')

            if resp.status_code == 404:
                return [TextContent(type='text', text=f'No cached data for {npdes}. Use seed_plant tool to seed it first.')]

            data = resp.json()
            seed = data.get('data', {})
            identity = seed.get('identity', {})
            basics = seed.get('overview', {}).get('basics', {})
            limits = seed.get('permitLimits', [])
            oh = seed.get('nymphLogs', {}).get('operatingHistory', [])

            violations = [e for e in oh if 'exceedance' in e.get('value', '').lower() or 'violation' in e.get('value', '').lower()]

            summary = f"""{identity.get('plantName')} — {npdes}
Location: {identity.get('city')}, {identity.get('state')}
Owner: {identity.get('owner')}
Process: {basics.get('processType')} | Flow: {basics.get('designFlowMGD')} MGD
Permit Parameters: {len(limits)}
Recent Violations: {len(violations)} recorded
Status: {seed.get('auditRun', {}).get('proposalStatus')}"""

            return [TextContent(type='text', text=summary)]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == '__main__':
    asyncio.run(main())
