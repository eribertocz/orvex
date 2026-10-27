import type { ErrorEnvelope, Health, Version } from '../contracts/http-api/v1';

/** An error answered by the server (an ErrorEnvelope) or a transport failure. */
export class ApiError extends Error {
	readonly code: string;
	readonly status: number;
	readonly requestId: string | null;
	readonly details: readonly { name: string; value: string }[];

	constructor(
		code: string,
		message: string,
		status: number,
		requestId: string | null,
		details: readonly { name: string; value: string }[] = []
	) {
		super(message);
		this.name = 'ApiError';
		this.code = code;
		this.status = status;
		this.requestId = requestId;
		this.details = details;
	}
}

function isErrorEnvelope(body: unknown): body is ErrorEnvelope {
	if (typeof body !== 'object' || body === null || !('error' in body)) return false;
	const error: unknown = body.error;
	return (
		typeof error === 'object' &&
		error !== null &&
		'code' in error &&
		typeof error.code === 'string' &&
		'message' in error &&
		typeof error.message === 'string'
	);
}

async function request<T>(path: string, fetcher: typeof fetch): Promise<T> {
	const response = await fetcher(path, { headers: { Accept: 'application/json' } });
	const requestId = response.headers.get('X-Request-Id');
	const body: unknown = await response.json().catch(() => null);
	if (response.ok) return body as T;
	if (isErrorEnvelope(body)) {
		const { code, message, details, request_id } = body.error;
		throw new ApiError(code, message, response.status, request_id, details);
	}
	throw new ApiError('HTTP_ERROR', `HTTP ${String(response.status)}`, response.status, requestId);
}

/** Typed access to the HTTP API (contracts/http-api/v1). */
export function createClient(fetcher: typeof fetch = fetch) {
	return {
		health: () => request<Health>('/api/v1/health', fetcher),
		version: () => request<Version>('/api/v1/version', fetcher)
	};
}
