import { CSSProperties, FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  Badge,
  Group,
  List,
  MantineColor,
  Stack,
  Text,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { faMinus, faPlus } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useEpisodeSubtitleModification,
  useSubtitleFileDownload,
} from "@/apis/hooks";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { CombinedSubtitleBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { BuildKey, toPython } from "@/utilities";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  getSyncEngineLabel,
  isCombinedOutputSubtitle,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputSubtitle,
  sortSyncOutputSubtitles,
} from "@/utilities/subtitles";

// Match quality for a downloaded/embedded subtitle, sourced from the episode
// history and shown inside the badge (mirrors the movie detail Score column).
export interface SubtitleScoreInfo {
  // Percentage string as the history API returns it, e.g. "98.0%".
  score?: string;
  provider?: string;
  matches?: string[];
  notMatches?: string[];
}

interface Props {
  seriesId: number;
  episodeId: number;
  // Owning Sonarr instance for this episode (#156); scopes subtitle
  // download/delete to the correct instance on multi-instance setups.
  arrInstanceId?: number;
  missing?: boolean;
  subtitle: Subtitle;
  availableSubtitles?: Subtitle[];
  // Present for downloaded/embedded subtitles that have a history record; the
  // badge then shows the score and a hover tooltip with the match details.
  scoreInfo?: SubtitleScoreInfo;
}

// Same thresholds as the movie detail ScoreBadge: green >= 90, yellow 70-89,
// red below 70.
function scoreBandColor(pct: number): MantineColor {
  return pct >= 90 ? "green" : pct >= 70 ? "yellow" : "red";
}

const ScoreTooltipLabel: FunctionComponent<{
  score: string;
  provider?: string;
  source: string;
  matches: string[];
  notMatches: string[];
}> = ({ score, provider, source, matches, notMatches }) => (
  <Stack gap={6}>
    <Text size="sm" fw={700}>
      Score: {score}
    </Text>
    {provider && <Text size="sm">Provider: {provider}</Text>}
    <Text size="sm" c="dimmed">
      {source}
    </Text>
    {(matches.length > 0 || notMatches.length > 0) && (
      <Group align="flex-start" justify="left" gap="xl" wrap="nowrap" grow>
        <Stack align="flex-start" gap={2}>
          <Text size="sm" c="green">
            <FontAwesomeIcon icon={faPlus} /> Matching
          </Text>
          <List size="sm" c="green">
            {matches.map((v, idx) => (
              <List.Item key={BuildKey(idx, v, "match")}>{v}</List.Item>
            ))}
          </List>
        </Stack>
        <Stack align="flex-start" gap={2}>
          <Text size="sm" c="yellow">
            <FontAwesomeIcon icon={faMinus} /> Not matching
          </Text>
          <List size="sm" c="yellow">
            {notMatches.map((v, idx) => (
              <List.Item key={BuildKey(idx, v, "miss")}>{v}</List.Item>
            ))}
          </List>
        </Stack>
      </Group>
    )}
  </Stack>
);

export function buildEpisodeSubtitleToolSelections({
  episodeId,
  arrInstanceId,
  missing,
  subtitle,
}: {
  episodeId: number;
  arrInstanceId?: number;
  missing: boolean;
  subtitle: Subtitle;
}): FormType.ModifySubtitle[] {
  if (missing) return [];

  const isEmbedded = !subtitle.path;
  return [
    {
      id: episodeId,
      type: "episode",
      path: isEmbedded ? "" : subtitle.path!,
      language: subtitle.code2,
      forced: toPython(subtitle.forced),
      hi: toPython(subtitle.hi),
      from_language: isEmbedded ? subtitle.code2 : undefined,
      arr_instance_id: arrInstanceId,
    },
  ];
}

function subtitleEditorUrl(
  action: "preview" | "edit",
  mediaType: "episode",
  mediaId: number,
  language: string,
  arrInstanceId?: number,
) {
  const path = `/subtitles/${action}/${mediaType}/${mediaId}/${encodeURIComponent(language)}`;
  return arrInstanceId !== undefined
    ? `${path}?arr_instance_id=${arrInstanceId}`
    : path;
}

export const Subtitle: FunctionComponent<Props> = ({
  seriesId,
  episodeId,
  arrInstanceId,
  missing = false,
  subtitle,
  availableSubtitles,
  scoreInfo,
}) => {
  const navigate = useNavigate();
  const { remove, download } = useEpisodeSubtitleModification();
  const combine = useCombineSubtitles();
  const fileDownload = useSubtitleFileDownload();

  const [opened, setOpen] = useState(false);
  const [compareOpened, setCompareOpened] = useState(false);

  // falsy path (null, undefined, "") means this is an embedded (in-container) subtitle track
  const isEmbedded = !subtitle.path;

  // Missing subtitles never carry a score. When a score is known the badge is
  // colored by its band and shows the percentage; otherwise it keeps its look.
  const scorePct = useMemo(() => {
    if (missing || !scoreInfo?.score) return undefined;
    const pct = parseFloat(scoreInfo.score);
    return Number.isNaN(pct) ? undefined : pct;
  }, [missing, scoreInfo]);
  const scoreColor =
    scorePct === undefined ? undefined : scoreBandColor(scorePct);
  const scored = scoreColor !== undefined;

  const variant: MantineColor | undefined = useMemo(() => {
    // A scored badge is painted by its band color (below) over the light
    // variant, which clears the custom variant styling first.
    if (scored) {
      return "light";
    } else if (opened && (missing || !isEmbedded)) {
      return "highlight";
    } else if (missing) {
      return "missing";
    } else if (isEmbedded) {
      return "disabled";
    }
  }, [isEmbedded, missing, opened, scored]);

  // The repo's badge.module.scss neutralizes the light variant's own color, so
  // paint the score band explicitly with Mantine's theme-aware light tokens.
  const scoreStyle: CSSProperties = scored
    ? {
        color: `var(--mantine-color-${scoreColor}-light-color)`,
        backgroundColor: `var(--mantine-color-${scoreColor}-light)`,
        border: "1px solid transparent",
      }
    : {};

  const selections = useMemo<FormType.ModifySubtitle[]>(() => {
    return buildEpisodeSubtitleToolSelections({
      episodeId,
      arrInstanceId,
      missing,
      subtitle,
    });
  }, [episodeId, arrInstanceId, missing, subtitle]);

  // Translation sources: all available subtitles (embedded + external).
  // For missing subs the menu shows "Translate from X" items.
  // Backend handles bitmap codec exclusion at extraction time.
  const translationSources = useMemo(
    () =>
      // Embedded tracks (empty path) are valid translate sources, so do not
      // filter on s.path; only exclude sync-output and combined-output subs.
      (availableSubtitles ?? []).filter(
        (s) => !isSyncOutputSubtitle(s) && !isCombinedOutputSubtitle(s),
      ),
    [availableSubtitles],
  );

  const syncOutputs = useMemo(
    () =>
      sortSyncOutputSubtitles(
        (availableSubtitles ?? []).filter((item) =>
          isCompatibleSyncOutputSubtitle(subtitle, item),
        ),
      ),
    [availableSubtitles, subtitle],
  );

  const canCompareSyncOutputs =
    !missing &&
    !isEmbedded &&
    !isSyncOutputSubtitle(subtitle) &&
    syncOutputs.length > 0;

  // The same language can show up twice in one row: once as a file on disk
  // and once as a track inside the video. The badges differ only in shade,
  // so name the source on hover.
  const badgeTitle = missing
    ? "Missing"
    : isEmbedded
      ? "Embedded in the video file"
      : `File: ${subtitle.path!.split(/[\\/]/).pop()}`;

  const languageBadge = (
    <Badge
      variant={variant}
      // The Mantine tooltip carries the source line when a score is shown, so
      // the native title would only duplicate it there.
      title={scored ? undefined : badgeTitle}
      style={{ whiteSpace: "nowrap", flexShrink: 0, ...scoreStyle }}
    >
      {/* Render inline (span) so an appended score stays on the same line
          instead of wrapping below the block <p> and being clipped. */}
      <Language.Text value={subtitle} long={false} span></Language.Text>
      {scored ? ` ${Math.round(scorePct!)}%` : null}
    </Badge>
  );

  const badgeEl = (
    <Group gap={4} wrap="nowrap">
      {scored ? (
        <Tooltip
          multiline
          // A fixed width is required: the global
          // [data-mantine-shared-portal-node] rule otherwise collapses a
          // multiline tooltip to min-content width.
          w={480}
          maw="90vw"
          withinPortal
          events={{ hover: true, focus: false, touch: true }}
          label={
            <ScoreTooltipLabel
              score={scoreInfo!.score!}
              provider={scoreInfo!.provider}
              source={badgeTitle}
              matches={scoreInfo!.matches ?? []}
              notMatches={scoreInfo!.notMatches ?? []}
            />
          }
        >
          {languageBadge}
        </Tooltip>
      ) : (
        languageBadge
      )}
      {isSyncOutputSubtitle(subtitle) && (
        <Badge
          color="gray"
          size="xs"
          variant="light"
          style={{ whiteSpace: "nowrap" }}
        >
          {getSyncEngineLabel(subtitle.modifier)}
        </Badge>
      )}
    </Group>
  );

  if (isCombinedOutputSubtitle(subtitle)) {
    const subtitlePath = subtitle.path;
    return (
      <Group gap={4} wrap="nowrap">
        <SubtitleToolsMenu
          selections={selections}
          isCombinedOutput
          menu={{
            trigger: "click",
            onOpen: () => setOpen(true),
            onClose: () => setOpen(false),
          }}
          onAction={async (action) => {
            if (action === "rebuild") {
              combine.mutate({
                scope: { kind: "episode", episodeId, arrInstanceId },
                body: combineRequestForSubtitle(subtitle) ?? {},
              });
            } else if (action === "view") {
              navigate(
                subtitleEditorUrl(
                  "preview",
                  "episode",
                  episodeId,
                  buildSubtitleLanguageKey(subtitle),
                  arrInstanceId,
                ),
              );
            } else if (action === "edit") {
              navigate(
                subtitleEditorUrl(
                  "edit",
                  "episode",
                  episodeId,
                  buildSubtitleLanguageKey(subtitle),
                  arrInstanceId,
                ),
              );
            } else if (action === "download") {
              fileDownload.mutate({
                type: "episode",
                mediaId: episodeId,
                language: buildSubtitleLanguageKey(subtitle),
                arrInstanceId,
              });
            } else if (action === "delete" && subtitlePath) {
              await remove.mutateAsync({
                seriesId,
                episodeId,
                arrInstanceId,
                form: {
                  language: subtitle.code2,
                  hi: subtitle.hi,
                  forced: subtitle.forced,
                  path: subtitlePath,
                },
              });
            }
          }}
        >
          <UnstyledButton aria-label="Combined subtitle">
            <CombinedSubtitleBadge subtitle={subtitle} />
          </UnstyledButton>
        </SubtitleToolsMenu>
      </Group>
    );
  }

  // Interactive badges: no Tooltip wrapper around the menu target
  // (Tooltip.Floating breaks Menu.Target click handling)
  const ctx = badgeEl;

  return (
    <>
      <SubtitleToolsMenu
        menu={{
          trigger: "click",
          onOpen: () => setOpen(true),
          onClose: () => setOpen(false),
        }}
        selections={selections}
        embeddedTrack={isEmbedded}
        canSync={canSynchronizeSubtitle(subtitle)}
        missingLanguage={missing ? subtitle : undefined}
        translationSources={missing ? translationSources : undefined}
        canCompareSyncOutputs={canCompareSyncOutputs}
        mediaId={episodeId}
        mediaType="episode"
        arrInstanceId={arrInstanceId}
        onAction={async (action) => {
          if (action === "view") {
            navigate(
              subtitleEditorUrl(
                "preview",
                "episode",
                episodeId,
                buildSubtitleLanguageKey(subtitle),
                arrInstanceId,
              ),
            );
          } else if (action === "edit") {
            navigate(
              subtitleEditorUrl(
                "edit",
                "episode",
                episodeId,
                buildSubtitleLanguageKey(subtitle),
                arrInstanceId,
              ),
            );
          } else if (action === "search") {
            await download.mutateAsync({
              seriesId,
              episodeId,
              arrInstanceId,
              form: {
                language: subtitle.code2,
                hi: subtitle.hi,
                forced: subtitle.forced,
              },
            });
          } else if (action === "compare-sync") {
            setCompareOpened(true);
          } else if (action === "download") {
            fileDownload.mutate({
              type: "episode",
              mediaId: episodeId,
              language: buildSubtitleLanguageKey(subtitle),
              arrInstanceId,
            });
          } else if (action === "delete" && subtitle.path) {
            await remove.mutateAsync({
              seriesId,
              episodeId,
              arrInstanceId,
              form: {
                language: subtitle.code2,
                hi: subtitle.hi,
                forced: subtitle.forced,
                path: subtitle.path,
              },
            });
          }
        }}
      >
        {ctx}
      </SubtitleToolsMenu>
      {canCompareSyncOutputs && (
        <SyncOutputCompareModal
          opened={compareOpened}
          onClose={() => setCompareOpened(false)}
          mediaType="episode"
          mediaId={episodeId}
          original={subtitle}
          outputs={syncOutputs}
        />
      )}
    </>
  );
};
