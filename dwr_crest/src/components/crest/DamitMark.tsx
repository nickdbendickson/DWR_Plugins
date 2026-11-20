import { useId } from "react";

/**
 * The DaMiT suite mark (dark colour mode): "Di" from the DaMiT wordmark (the D and the orange i) standing in the water.
 * Same artwork as public/favicon.svg and docs/brand/damit-mark-dark.svg, inlined so the web, desktop and one-file
 * builds need no extra asset. Decorative by default; pass a `title` to make it an image with a name.
 */
export function DamitMark({ className, title }: { className?: string; title?: string }) {
  const id = useId().replace(/:/g, "");
  const water = `damit-water-${id}`;
  const tile = `damit-tile-${id}`;
  return (
    <svg
      viewBox="0 0 32 32"
      className={className}
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <clipPath id={water}>
          <path d="M-4.0,21.3 Q-1.8,20.0 0.5,21.3 Q2.8,22.6 5.0,21.3 Q7.2,20.0 9.5,21.3 Q11.8,22.6 14.0,21.3 Q16.2,20.0 18.5,21.3 Q20.8,22.6 23.0,21.3 Q25.2,20.0 27.5,21.3 Q29.8,22.6 32.0,21.3 Q34.2,20.0 36.5,21.3 Q38.8,22.6 41.0,21.3 L40,-4 L-4,-4 Z" />
        </clipPath>
        <clipPath id={tile}>
          <rect width="32" height="32" rx="7" />
        </clipPath>
      </defs>
      <rect width="32" height="32" rx="7" fill="#2a2b30" />
      <g clipPath={`url(#${tile})`}>
        <g clipPath={`url(#${water})`}>
          <path
            fill="#30c4c8"
            d="M11.82 26C17.29 26 20.68 22.62 20.68 17C20.68 11.38 17.29 8 11.82 8H5.2V26ZM9.46 22.35V11.65H11.58C14.73 11.65 16.41 13.1 16.41 17C16.41 20.9 14.73 22.35 11.64 22.35Z"
          />
          <path
            fill="#f49e18"
            d="M22.58 26H26.8V12.49H22.58ZM24.69 10.91C25.89 10.91 26.87 10.01 26.87 8.88C26.87 7.76 25.89 6.85 24.69 6.85C23.49 6.85 22.51 7.76 22.51 8.88C22.51 10.01 23.49 10.91 24.69 10.91Z"
          />
        </g>
        <path
          fill="none"
          stroke="#1fbcbc"
          strokeWidth="3.4"
          d="M-4.0,24.0 Q-1.8,22.7 0.5,24.0 Q2.8,25.3 5.0,24.0 Q7.2,22.7 9.5,24.0 Q11.8,25.3 14.0,24.0 Q16.2,22.7 18.5,24.0 Q20.8,25.3 23.0,24.0 Q25.2,22.7 27.5,24.0 Q29.8,25.3 32.0,24.0 Q34.2,22.7 36.5,24.0 Q38.8,25.3 41.0,24.0"
        />
        <path
          fill="none"
          stroke="#a8e7ec"
          strokeWidth="0.9"
          d="M-4.0,24.2 Q-1.8,22.9 0.5,24.2 Q2.8,25.5 5.0,24.2 Q7.2,22.9 9.5,24.2 Q11.8,25.5 14.0,24.2 Q16.2,22.9 18.5,24.2 Q20.8,25.5 23.0,24.2 Q25.2,22.9 27.5,24.2 Q29.8,25.5 32.0,24.2 Q34.2,22.9 36.5,24.2 Q38.8,25.5 41.0,24.2"
        />
        <path
          fill="none"
          stroke="#1fbcbc"
          strokeWidth="2.4"
          opacity=".5"
          d="M-4.0,28.6 Q-1.8,27.3 0.5,28.6 Q2.8,29.9 5.0,28.6 Q7.2,27.3 9.5,28.6 Q11.8,29.9 14.0,28.6 Q16.2,27.3 18.5,28.6 Q20.8,29.9 23.0,28.6 Q25.2,27.3 27.5,28.6 Q29.8,29.9 32.0,28.6 Q34.2,27.3 36.5,28.6 Q38.8,29.9 41.0,28.6"
        />
      </g>
    </svg>
  );
}
