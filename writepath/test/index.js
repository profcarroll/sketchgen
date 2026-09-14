/**
 * Entry point for `node --test writepath/test/`.
 *
 * This Node build resolves a directory argument as a module rather than walking
 * it for test files, so the directory needs an index that pulls every suite in.
 * `node --test` with no argument, and `node --test writepath/test/*.test.js`,
 * both work without it. Add one line here per new suite.
 */

import "./worker.test.js";
