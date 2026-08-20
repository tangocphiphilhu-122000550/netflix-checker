import countries from "@/data/countries.json";

const MAP = countries as Record<string, string>;

/** VN → Vietnam · US → United States · unknown → original text */
export function countryName(code?: string | null): string {
  const raw = (code || "").trim();
  if (!raw) return "—";
  const key = raw.toUpperCase();
  // already a full name (has space or length > 3)
  if (MAP[key]) return MAP[key];
  if (raw.length > 3) return raw;
  return raw;
}

/** Show "Vietnam (VN)" when mapped, else raw / — */
export function countryLabel(code?: string | null, withCode = true): string {
  const raw = (code || "").trim();
  if (!raw) return "—";
  const key = raw.toUpperCase();
  const name = MAP[key];
  if (!name) return raw;
  if (!withCode || name.toUpperCase() === key) return name;
  return `${name} (${key})`;
}
