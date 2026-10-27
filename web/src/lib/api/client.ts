import type { ErrorDetail, ErrorEnvelope, Health, Version } from '../contracts/http-api/v1';

/**
 * Every failure of an API call. `code` is a server code from the http-api registry, or one of the
 * client-only codes HTTP_ERROR, INVALID_RESPONSE and NETWORK_ERROR (contracts/http-api/v1).
 */
export class ApiError extends Error {
	readonly code: string;
	readonly status: number;
	readonly requestId: string | null;
	readonly details: readonly ErrorDetail[];

	constructor(
		code: string,
		message: string,
		status: number,
		requestId: string | null,
		details: readonly ErrorDetail[] = []
	) {
		super(message);
		this.name = 'ApiError';
		this.code = code;
		this.status = status;
		this.requestId = requestId;
		this.details = details;
	}
}

function isObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isErrorEnvelope(body: unknown): body is ErrorEnvelope {
	if (!isObject(body) || !isObject(body.error)) return false;
	const { code, message, details, request_id } = body.error;
	return (
		typeof code === 'string' &&
		typeof message === 'string' &&
		typeof request_id === 'string' &&
		Array.isArray(details) &&
		details.every(
			(detail) =>
				isObject(detail) && typeof detail.name === 'string' && typeof detail.value === 'string'
		)
	);
}

async function request<T>(path: string, fetcher: typeof fetch): Promise<T> {
	let response: Response;
	try {
		response = await fetcher(path, { headers: { Accept: 'application/json' } });
	} catch (error) {
		const reason = error instanceof Error ? error.message : String(error);
		throw new ApiError('NETWORK_ERROR', `The server could not be reached: ${reason}`, 0, null);
	}
	const requestId = response.headers.get('X-Request-Id');
	const body: unknown = await response.json().catch(() => undefined);
	if (response.ok) {
		if (!isObject(body)) {
			const status = String(response.status);
			throw new ApiError(
				'INVALID_RESPONSE',
				`HTTP ${status} without a JSON object`,
				response.status,
				requestId
			);
		}
		return body as T;
	}
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
