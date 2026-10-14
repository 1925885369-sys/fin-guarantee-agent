import type { EntityProfileFact } from "./contracts";

const MUNICIPALITIES = /^(北京市|上海市|天津市|重庆市)/;
const PROVINCE_CITY_DISTRICT = /^(.+?(?:省|自治区))(.+?(?:市|自治州|地区|盟))(.+?(?:区|县|旗|自治县))/;
const PROVINCE_COUNTY = /^(.+?(?:省|自治区))(.+?(?:市|自治州|地区|盟))?(.+?(?:县|旗|自治县))/;
const PROVINCE_CITY = /^(.+?(?:省|自治区))(.+?(?:市|自治州|地区|盟))/;

/**
 * Return only a confidently recognised province/city/county-level location.
 * The source value stays unchanged in the evidence model; this is display-only.
 */
export function administrativeAddress(value: string): string {
  const compact = value.replace(/\s+/g, "").trim();
  if (!compact) return "待补充";
  if (MUNICIPALITIES.test(compact)) {
    const municipality = compact.match(MUNICIPALITIES)?.[1] || "";
    const district = compact.slice(municipality.length).match(/^(.+?(?:区|县))/)?.[1];
    return district ? `${municipality}${district}` : municipality;
  }
  const full = compact.match(PROVINCE_CITY_DISTRICT);
  if (full) return `${full[1]}${full[2]}${full[3]}`;
  const county = compact.match(PROVINCE_COUNTY);
  if (county?.[1] && county?.[3]) return `${county[1]}${county[2] || ""}${county[3]}`;
  const city = compact.match(PROVINCE_CITY);
  if (city) return `${city[1]}${city[2]}`;
  return "地址层级待核验";
}

export function factForKey(facts: EntityProfileFact[], factKey: string): EntityProfileFact | undefined {
  return facts.find((fact) => fact.fact_key === factKey && fact.status !== "insufficient");
}

function annualPeriod(period: string | null): string | null {
  if (!period || /Q|季度|半年|月|一期/i.test(period)) return null;
  const year = period.match(/(?:19|20)\d{2}/)?.[0];
  return year || null;
}

export function latestAnnualPeriods(facts: EntityProfileFact[], limit = 3): string[] {
  return [...new Set(facts.map((fact) => annualPeriod(fact.period)).filter((period): period is string => Boolean(period)))]
    .sort((left, right) => right.localeCompare(left))
    .slice(0, limit)
    .reverse();
}

export function financialFact(
  facts: EntityProfileFact[], factKey: string, year: string,
): EntityProfileFact | undefined {
  const matches = facts.filter((fact) => fact.fact_key === factKey && annualPeriod(fact.period) === year);
  return matches.find((fact) => fact.scope === "consolidated" && fact.status !== "insufficient")
    || matches.find((fact) => fact.status !== "insufficient")
    || matches[0];
}
