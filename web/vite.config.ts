import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import tailwindcss from '@tailwindcss/vite';
import { svelteTesting } from '@testing-library/svelte/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
	// The licenses of every dependency bundled into the build, shipped with it (#47).
	build: { license: { fileName: 'THIRD_PARTY_LICENSES.md' } },
	plugins: [
		tailwindcss(),
		sveltekit({
			compilerOptions: { runes: true },
			// Static files only: the server serves them and answers client-side routes with the
			// fallback page (P0-07).
			adapter: adapter({ fallback: 'index.html' })
		}),
		svelteTesting()
	],
	test: {
		environment: 'jsdom',
		include: ['src/**/*.test.ts']
	}
});
