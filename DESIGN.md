---
version: alpha
name: xPST Design System
description: A calm, clear control plane for cross-posting work. The system favors readable hierarchy, honest state, and platform-neutral clarity over decorative imitation.
colors:
  primary: "#0066CC"
  secondary: "#525866"
  tertiary: "#E85D3F"
  neutral: "#F5F7FA"
  background: "#F5F7FA"
  surface: "#FFFFFF"
  surfaceRaised: "#FFFFFF"
  text: "#1B1D21"
  textSecondary: "#525866"
  textMuted: "#667085"
  success: "#197A43"
  warning: "#8A5A00"
  danger: "#B42318"
  focus: "#005FCC"
typography:
  display:
    fontFamily: Inter
    fontSize: 2.25rem
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  heading:
    fontFamily: Inter
    fontSize: 1.5rem
    fontWeight: 700
    lineHeight: 1.25
  body:
    fontFamily: Inter
    fontSize: 1rem
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: Inter
    fontSize: 0.875rem
    fontWeight: 600
    lineHeight: 1.25
  caption:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: 400
    lineHeight: 1.35
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
  3xl: 48px
rounded:
  none: 0px
  sm: 6px
  md: 10px
  lg: 14px
  pill: 999px
elevation:
  card: "0 1px 2px rgba(16, 24, 40, 0.06)"
  raised: "0 8px 24px rgba(16, 24, 40, 0.10)"
  overlay: "0 16px 40px rgba(16, 24, 40, 0.16)"
motion: "standard=180ms; emphasis=240ms; easing=cubic-bezier(0.2, 0, 0, 1)"
focus: "ring={colors.focus} 3px with a 2px offset"
themes: "light defaults with semantic dark overrides; see tokens.css"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#FFFFFF"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 12px
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: 12px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
  status-error:
    backgroundColor: "#FDECEC"
    textColor: "{colors.danger}"
    rounded: "{rounded.pill}"
  status-success:
    backgroundColor: "#EAF7EF"
    textColor: "{colors.success}"
    rounded: "{rounded.pill}"
  status-warning:
    backgroundColor: "#FFF7DF"
    textColor: "{colors.warning}"
    rounded: "{rounded.pill}"
  status-disabled:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.textMuted}"
    rounded: "{rounded.pill}"
  card-raised:
    backgroundColor: "{colors.surfaceRaised}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
  secondary-label:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.textSecondary}"
    rounded: "{rounded.sm}"
  focus-indicator:
    backgroundColor: "{colors.focus}"
    textColor: "#FFFFFF"
    rounded: "{rounded.sm}"
---

## Overview

xPST is a cross-posting control plane, not a marketing canvas. Its interface should help a person understand what is ready, what needs attention, and what happened without making them decode decoration. The visual language follows Apple Human Interface Guidelines principles of **clarity**, **deference**, and **depth**: content and status lead, controls are familiar, and depth is reserved for grouping and priority. This is a set of transferable principles, not a visual copy of any Apple product.

The system is local-first and offline-safe. Assets are bundled, data freshness is stated where relevant, and no component assumes a network connection or external telemetry.

## Colors

- **Primary (`#0066CC`):** An accessible action blue for the main action on light surfaces. Dark mode uses a lighter semantic value so the same role remains legible.
- **Text:** `text` is for readable content, `textSecondary` supports hierarchy, and `textMuted` is only for non-essential context. Never use color alone to communicate state.
- **Status:** Success, warning, and danger colors describe state and are paired with a label or icon. Avoid platform colors for health or workflow state.
- **Surfaces:** Background separates the workspace from cards; surface and surfaceRaised group related content without gradients or glass effects.

## Typography

Inter is self-hosted and used consistently. Use the display and heading styles for hierarchy, body for readable content, label for controls, and caption only for supporting context. Prefer sentence case, short labels, and explicit verbs. Do not use all caps for whole messages or rely on weight alone to communicate an error.

## Layout

Use the spacing scale in multiples of 4px. Start with a single-column reading order and add columns only when the content benefits from comparison. The responsive shell keeps navigation available on small screens, uses a 60rem content measure where possible, and allows tables to scroll rather than forcing tiny text. Keep primary actions near the content they affect.

## Elevation & Depth

Use `card` elevation for persistent groups, `raised` for a transient focus area, and `overlay` only for an actual overlay. Borders and spacing should do most of the grouping work. Avoid stacked shadows, ornamental gradients, and elevation that suggests an action is clickable when it is not.

## Shapes

Use `sm` for fields and compact controls, `md` for buttons, `lg` for cards, and `pill` for status badges only. Consistent radii make the system easier to scan. Do not use rounded containers as decoration around every line of text.

## Components

Primitives expose semantic state and accessible names so pages do not reinvent interaction rules. `Button` has visible focus, disabled, and loading states. `Card` groups content. `StatusBadge` pairs a label with state. `PlatformBadge` identifies a destination with the shared Lucide icon family. `EmptyState`, `LoadingSkeleton`, and `ErrorState` explain what is happening and provide the next safe action. `FormField` always connects its label, hint, and error text. `Shell` and `Nav` preserve a predictable reading order across viewport sizes.

A loading state should reserve the shape of the content and announce a concise status. An error state should say what failed and offer retry when retrying is safe. An empty state should distinguish “nothing exists yet” from “data could not be loaded.”

## Do's and Don'ts

### Do

- Do lead with the user’s current status and the next useful action.
- Do preserve keyboard order, visible focus, and a 44px minimum target for primary controls.
- Do use labels and text alongside color, icons, or position for state.
- Do show honest loading, empty, error, and freshness states.
- Do keep actions reversible or explicit; destructive actions need a clear confirmation boundary.
- Do prefer local assets and deterministic rendering so the UI works offline.
- Do use familiar platform-neutral icons from one permissively licensed family.

### Don't

- Don’t copy Apple-specific visuals, terminology, or branded platform controls.
- Don’t use gradients, blur, or shadows to manufacture hierarchy that spacing and typography can provide.
- Don’t hide important status in tooltips, hover-only affordances, or color alone.
- Don’t replace an actionable error with a blank panel or an endless spinner.
- Don’t show fabricated zeroes when data is unavailable or stale.
- Don’t add a new workflow to a presentation primitive; pages should call existing API contracts.
- Don’t load fonts, icons, telemetry, or scripts from a remote CDN.
