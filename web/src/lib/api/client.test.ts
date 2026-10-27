import { describe, expect, it } from 'vitest';

import { ApiError, createClient } from './client';

function respond(status: number, body: string, headers: Record<string, string> = {}): typeof fetch {
	return () => Promise.resolve(new Response(body, { status, headers }));
}

describe('HTTP API client', () => {
	it('returns typed data for 200 responses', async () => {
		const client = createClient(respond(200, '{"status":"ok"}'));
		await expect(client.health()).resolves.toEqual({ status: 'ok' });
	});

	it('turns an ErrorEnvelope into an ApiError with code and request id', async () => {
		const envelope = {
			error: { code: 'NOT_FOUND', message: 'No route', details: [], request_id: 'r-1' }
		};
		const client = createClient(respond(404, JSON.stringify(envelope)));
		const error: unknown = await client.version().catch((caught: unknown) => caught);
		expect(error).toBeInstanceOf(ApiError);
		expect(error).toMatchObject({ code: 'NOT_FOUND', status: 404, requestId: 'r-1' });
	});

	it('reports a non-JSON error as HTTP_ERROR with the header request id', async () => {
		const client = createClient(respond(502, 'Bad gateway', { 'X-Request-Id': 'r-2' }));
		const error: unknown = await client.health().catch((caught: unknown) => caught);
		expect(error).toMatchObject({ code: 'HTTP_ERROR', status: 502, requestId: 'r-2' });
	});
});
