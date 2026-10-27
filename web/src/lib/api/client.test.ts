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

describe('HTTP API client failure modes', () => {
	it('rejects a 200 response that is not a JSON object with INVALID_RESPONSE', async () => {
		const client = createClient(respond(200, '<html>', { 'X-Request-Id': 'r-3' }));
		const error: unknown = await client.health().catch((caught: unknown) => caught);
		expect(error).toMatchObject({ code: 'INVALID_RESPONSE', status: 200, requestId: 'r-3' });
	});

	it('treats an incomplete envelope as HTTP_ERROR and keeps the header request id', async () => {
		const client = createClient(
			respond(500, '{"error":{"code":"X","message":"m"}}', { 'X-Request-Id': 'hdr' })
		);
		const error: unknown = await client.health().catch((caught: unknown) => caught);
		expect(error).toMatchObject({ code: 'HTTP_ERROR', status: 500, requestId: 'hdr' });
	});

	it('wraps a network failure as NETWORK_ERROR', async () => {
		const offline: typeof fetch = () => Promise.reject(new TypeError('Failed to fetch'));
		const error: unknown = await createClient(offline)
			.health()
			.catch((caught: unknown) => caught);
		expect(error).toBeInstanceOf(ApiError);
		expect(error).toMatchObject({ code: 'NETWORK_ERROR', status: 0, requestId: null });
	});
});
