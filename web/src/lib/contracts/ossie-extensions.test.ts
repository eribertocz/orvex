import { describe, expectTypeOf, it } from 'vitest';

import type { DatasetPayload, FieldPayload, OssieExtensionPayload } from './ossie-extensions/v1';

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
