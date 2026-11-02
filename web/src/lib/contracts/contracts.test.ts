import { describe, expectTypeOf, it } from 'vitest';

import type { ExampleReading } from './_example/v1';
import type { DatasetPayload, FieldPayload, OssieExtensionPayload } from './ossie-extensions/v1';
import type { Cell, Column, StateCell } from './semantic-result/v1';

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

describe('ossie-extensions v1 types', () => {
	it('keeps exact values and numeric shapes as text (law 11)', () => {
		expectTypeOf<NonNullable<DatasetPayload['bridge']>['total']>().toEqualTypeOf<string>();
		expectTypeOf<NonNullable<FieldPayload['numeric']>['precision']>().toEqualTypeOf<
			string | undefined
		>();
	});

	it('accepts one payload of each element', () => {
		const payloads: OssieExtensionPayload[] = [
			{ version: 1, element: 'model', default_time_zone: 'America/La_Paz' },
			{
				version: 1,
				element: 'dataset',
				entity: 'order_campaign',
				bridge: {
					left: 'order',
					right: 'campaign',
					weight: 'allocation_weight',
					total: '1.00',
					tolerance: '0.00',
					normalization_scope: { entities: ['order'] }
				}
			},
			{ version: 1, element: 'field', semantic_type: 'amount', currency: 'BOB' },
			{
				version: 1,
				element: 'metric',
				window: {
					function: 'running_total',
					of: 'revenue',
					partition_by: [],
					order_by: ['month'],
					densify: 'calendar'
				}
			},
			{ version: 1, element: 'relationship', cardinality: 'many_to_one' }
		];
		expectTypeOf(payloads).toEqualTypeOf<OssieExtensionPayload[]>();
	});
});

describe('semantic-result v1 types', () => {
	it('types an unknown cell state as a state, never as a value or null', () => {
		expectTypeOf<{ state: 'future_state' }>().toExtend<Cell>();
		expectTypeOf<StateCell>().not.toExtend<string | number | boolean | null>();
		expectTypeOf<Extract<Cell, object>>().toEqualTypeOf<StateCell>();
	});

	it('keeps exact cells as text and a column type separate from its encoding', () => {
		expectTypeOf<Extract<Cell, string>>().toEqualTypeOf<string>();
		expectTypeOf<Column['encoding']>().toEqualTypeOf<string>();
	});
});
