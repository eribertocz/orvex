import { render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import AppFrame from './AppFrame.svelte';

describe('AppFrame', () => {
	it('renders a single main landmark', () => {
		render(AppFrame);
		expect(screen.getAllByRole('main')).toHaveLength(1);
	});
});
