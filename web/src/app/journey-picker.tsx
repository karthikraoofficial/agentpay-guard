import Link from "next/link";

import { GROUPS, SCENARIOS, type Scenario } from "@/lib/scenarios";

// Every journey, grouped A–H. With `onChoose` each is a button (the /demo page); without,
// each links to /demo?scenario=<id> (the home page).
export function JourneyPicker({ onChoose }: { onChoose?: (s: Scenario) => void }) {
  if (SCENARIOS.length === 0) return <p>No journeys in this build.</p>;
  const box =
    "block w-full rounded-md border border-neutral-300 p-3 text-left hover:border-neutral-500 dark:border-neutral-700";
  return (
    <>
      {GROUPS.map((group) => {
        const inGroup = SCENARIOS.filter((s) => s.group === group.id);
        return (
          <section key={group.id} className="flex flex-col gap-2">
            <h2 className="font-semibold">
              {group.id}. {group.name}
            </h2>
            {inGroup.length === 0 ? (
              <p className="text-sm text-neutral-500">Coming soon.</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {inGroup.map((s) => {
                  const body = (
                    <>
                      <span className="font-medium">
                        {s.id}. {s.title}
                      </span>
                      <span className="mt-1 block text-sm text-neutral-600 dark:text-neutral-400">{s.story}</span>
                    </>
                  );
                  return (
                    <li key={s.id}>
                      {onChoose ? (
                        <button type="button" onClick={() => onChoose(s)} className={box}>
                          {body}
                        </button>
                      ) : (
                        <Link href={`/demo?scenario=${s.id}`} className={box}>
                          {body}
                        </Link>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        );
      })}
    </>
  );
}
