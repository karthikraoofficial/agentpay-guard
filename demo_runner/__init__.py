"""The Demo runner (#273, ADR-0031): a small server beside the API that plays Scenarios for
the web app's /demo page, one step per browser request. It holds the Demo Tenant's keys
so the browser never does, talks to the API over HTTP only, and makes no model call of
its own: an Intent check is POST /v1/intents under the Demo Tenant's Model spend cap."""
