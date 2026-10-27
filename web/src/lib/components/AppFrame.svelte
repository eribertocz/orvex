<script lang="ts">
	import type { Snippet } from 'svelte';

	let { children }: { children?: Snippet } = $props();
</script>

<main class="min-h-dvh bg-white text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
	{@render children?.()}
</main>
