// The integrated app always uses the 1.0 backend. Historical mockEngine is a
// UI fixture only; connection failures never fall back to mock.
export { realApi as api, ApiError } from './client'
