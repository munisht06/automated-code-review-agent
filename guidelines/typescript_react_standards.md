# TypeScript & React Standards

## TypeScript Configuration

- Use TypeScript strict mode (`"strict": true` in tsconfig.json)
- Define explicit types for all function parameters and return values
- Avoid using `any` type - use `unknown` if type is truly unknown
- Use union types and type guards for type narrowing
- Leverage utility types: `Partial<T>`, `Required<T>`, `Pick<T>`, `Omit<T>`

## React Component Guidelines

- Use functional components with hooks instead of class components
- Keep components small and focused (max 300 lines)
- Extract complex logic into custom hooks
- Use proper prop types with TypeScript interfaces
- Implement proper error boundaries for component trees

## State Management

- Use `useState` for local component state
- Use `useReducer` for complex state logic
- Use `useContext` for shared state across component tree
- Consider Redux/Zustand for global application state
- Keep state as close to where it's used as possible

## Performance Optimization

- Memoize expensive computations with `useMemo`
- Memoize callback functions with `useCallback`
- Use `React.memo()` for expensive components that receive same props
- Avoid inline function definitions in JSX props
- Use proper dependency arrays in `useEffect` and other hooks

## Hooks Best Practices

- Follow the Rules of Hooks (only call at top level, only call from React functions)
- Use exhaustive dependency arrays in `useEffect`
- Clean up side effects in `useEffect` return function
- Don't call hooks conditionally
- Extract reusable logic into custom hooks

## Styling

- Use CSS Modules or styled-components for component-scoped styles
- Follow BEM naming convention if using plain CSS
- Use Tailwind utility classes consistently
- Avoid inline styles except for dynamic values
- Keep styling logic separate from business logic

## Security

- Sanitize user input before rendering
- Avoid `dangerouslySetInnerHTML` unless absolutely necessary
- Use DOMPurify if rendering user-provided HTML is required
- Validate all form inputs on both client and server
- Implement proper CSRF protection for forms

## Testing

- Write unit tests for business logic and utility functions
- Write component tests using React Testing Library
- Test user interactions, not implementation details
- Mock external API calls and dependencies
- Aim for 80%+ test coverage on critical paths

## Code Organization

- One component per file
- Co-locate related files (component, styles, tests)
- Use barrel exports (index.ts) for cleaner imports
- Organize by feature, not by file type
- Keep shared utilities in a common directory
