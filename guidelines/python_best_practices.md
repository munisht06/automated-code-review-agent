# Python Best Practices

## Code Style

- Follow PEP 8 for code formatting and style conventions
- Use meaningful variable and function names that describe their purpose
- Keep functions small and focused on a single responsibility (max 50 lines)
- Use type hints for all function parameters and return values
- Write docstrings for all public classes, methods, and functions using Google or NumPy style

## Error Handling

- Use specific exception types rather than bare `except:` clauses
- Always provide context when raising exceptions
- Clean up resources using `try/finally` or context managers (`with` statements)
- Log exceptions with full traceback information
- Don't swallow exceptions silently

## Security

- Never hardcode passwords, API keys, or secrets in code
- Use environment variables or secret management services
- Validate and sanitize all user inputs
- Use parameterized queries to prevent SQL injection
- Avoid using `eval()` or `exec()` with user-provided data

## Performance

- Use list comprehensions for simple transformations (more Pythonic and faster)
- Leverage built-in functions and standard library when possible
- Use generators for large datasets to save memory
- Profile before optimizing - avoid premature optimization
- Use appropriate data structures (sets for membership tests, defaultdict for counting)

## Testing

- Write unit tests for all business logic
- Aim for at least 80% code coverage
- Use pytest fixtures to reduce test code duplication
- Mock external dependencies in unit tests
- Write integration tests for critical user flows

## Async/Await

- Use `async/await` for I/O-bound operations (API calls, database queries, file operations)
- Don't use async for CPU-bound operations
- Always await coroutines - don't forget the `await` keyword
- Use `asyncio.gather()` for parallel execution of independent tasks
- Handle exceptions properly in async code using `try/except`
