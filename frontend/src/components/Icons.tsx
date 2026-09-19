/**
 * The icon set.
 *
 * Hand-rolled inline SVG rather than an icon package: it keeps the bundle
 * dependency-free, inherits `currentColor` so icons follow the theme for free,
 * and there is no font or sprite to load before the first paint.
 */

import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconFlask = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 3v6.2L4.6 17A2.4 2.4 0 0 0 6.7 20.6h10.6A2.4 2.4 0 0 0 19.4 17L15 9.2V3" />
    <path d="M7.5 3h9M6.4 14h11.2" />
  </Svg>
);

export const IconSpark = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3.2 13.9 9l5.8 1.9-5.8 1.9L12 18.6 10.1 12.8 4.3 10.9 10.1 9z" />
    <path d="M18.5 3.4v3M20 4.9h-3M5.5 17.4v2.4M6.7 18.6H4.3" />
  </Svg>
);

export const IconRuns = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 6h16M4 12h16M4 18h9" />
    <circle cx="19" cy="18" r="2.2" />
  </Svg>
);

export const IconDocument = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 3H7.4A1.4 1.4 0 0 0 6 4.4v15.2A1.4 1.4 0 0 0 7.4 21h9.2a1.4 1.4 0 0 0 1.4-1.4V7z" />
    <path d="M14 3v4h4M9 13h6M9 17h4" />
  </Svg>
);

export const IconGauge = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 18a8 8 0 1 1 16 0" />
    <path d="M12 18l4-5" />
  </Svg>
);

export const IconShield = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 5 5.8v5.4c0 4.2 2.9 8.1 7 9.8 4.1-1.7 7-5.6 7-9.8V5.8z" />
    <path d="m9.3 12 1.9 1.9 3.6-3.7" />
  </Svg>
);

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="6.4" />
    <path d="m16 16 4 4" />
  </Svg>
);

export const IconBolt = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13.2 3 5.5 13.2h5.3L10 21l7.7-10.2h-5.3z" />
  </Svg>
);

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="m4.5 12.5 4.6 4.6L19.5 6.7" />
  </Svg>
);

export const IconX = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Svg>
);

export const IconAlert = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3.6 2.9 19.2a1.3 1.3 0 0 0 1.1 2h16a1.3 1.3 0 0 0 1.1-2z" />
    <path d="M12 9.6v4.2M12 17.4h.01" />
  </Svg>
);

export const IconInfo = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </Svg>
);

export const IconArrowRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 12h13M13 6.5 18.5 12 13 17.5" />
  </Svg>
);

export const IconArrowLeft = (p: IconProps) => (
  <Svg {...p}>
    <path d="M19 12H6M11 6.5 5.5 12 11 17.5" />
  </Svg>
);

export const IconUpload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 16V4M7.5 8.5 12 4l4.5 4.5" />
    <path d="M4.5 15.5v3a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5v-3" />
  </Svg>
);

export const IconLogout = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10 20H6a1.6 1.6 0 0 1-1.6-1.6V5.6A1.6 1.6 0 0 1 6 4h4" />
    <path d="M15.5 8.2 19.5 12l-4 3.8M19 12H9.5" />
  </Svg>
);

export const IconSun = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="4.2" />
    <path d="M12 2.6v2M12 19.4v2M2.6 12h2M19.4 12h2M5.4 5.4l1.4 1.4M17.2 17.2l1.4 1.4M18.6 5.4l-1.4 1.4M6.8 17.2l-1.4 1.4" />
  </Svg>
);

export const IconMoon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 14.2A8.4 8.4 0 0 1 9.8 4 8.4 8.4 0 1 0 20 14.2" />
  </Svg>
);

export const IconClock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="8.6" />
    <path d="M12 7.2V12l3 1.8" />
  </Svg>
);

export const IconGraph = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="6" cy="6.5" r="2.6" />
    <circle cx="18" cy="12" r="2.6" />
    <circle cx="6" cy="17.5" r="2.6" />
    <path d="M8.3 7.8 15.7 11M15.7 13.2 8.3 16.3" />
  </Svg>
);

export const IconQuote = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9.4 6.5C6.8 7.7 5.2 10 5.2 12.9c0 2.5 1.5 4.6 3.7 4.6 1.8 0 3.1-1.3 3.1-3.1 0-1.7-1.2-3-2.8-3-.3 0-.6 0-.9.1.3-1.6 1.4-2.9 3-3.6z" />
    <path d="M19 6.5c-2.6 1.2-4.2 3.5-4.2 6.4 0 2.5 1.5 4.6 3.7 4.6 1.8 0 3.1-1.3 3.1-3.1 0-1.7-1.2-3-2.8-3-.3 0-.6 0-.9.1.3-1.6 1.4-2.9 3-3.6z" />
  </Svg>
);

export const IconLock = (p: IconProps) => (
  <Svg {...p}>
    <rect x="4.8" y="10.4" width="14.4" height="10" rx="2" />
    <path d="M8.2 10.4V7.6a3.8 3.8 0 0 1 7.6 0v2.8" />
  </Svg>
);

export const IconWand = (p: IconProps) => (
  <Svg {...p}>
    <path d="m4 20 9.8-9.8M15.4 5.2l3.4 3.4" />
    <path d="M17 3v3.4M20.6 6.6h-3.4M9 4.4v2.4M10.2 5.6H7.8" />
  </Svg>
);
