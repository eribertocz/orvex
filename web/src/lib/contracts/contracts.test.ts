import { describe, expectTypeOf, it } from 'vitest';

import type { ExampleReading } from './_example/v1';

describe('generated contract types', () => {
	it('keeps exact decimals as strings, never numbers (law 11)', () => {
		expectTypeOf<ExampleReading['kwh']>().toEqualTypeOf<string>();
		expectTypeOf<ExampleReading['kwh']>().not.toEqualTypeOf<number>();
	});

	it('keeps enums as unions of literals and nullable fields as null', () => {
		expectTypeOf<ExampleReading['status']>().toEqualTypeOf<'valid' | 'estimated' | 'voided'>();
		expectTypeOf<ExampleReading['note']>().toEqualTypeOf<string | null | undefined>();
	});
});
