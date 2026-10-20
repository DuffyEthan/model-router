// Test fixture: the price rows of hooks/prices.json the plugin reads, as its text.
export const PRICES_JSON = JSON.stringify({
  classifier_model: 'claude-haiku-5-5',
  models: {
    'claude-opus-5-5': { input: 4, cache_write_5m: 5, cache_write_1h: 8, cache_read: 0.2, output: 20 },
    'claude-sonnet-5-5': { input: 2, cache_write_5m: 2.5, cache_write_1h: 4, cache_read: 0.1, output: 10 },
    'claude-haiku-5-5': {
      input: 0.1, cache_write_5m: 0.125, cache_write_1h: 0.2, cache_read: 0.01, output: 0.5,
      long_context: {
        over_prompt_tokens: 100000,
        input: 0.5, cache_write_5m: 0.625, cache_write_1h: 1, cache_read: 0.05, output: 2.5,
      },
    },
  },
})
