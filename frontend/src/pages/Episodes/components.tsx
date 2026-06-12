import { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, Group, MantineColor, UnstyledButton } from "@mantine/core";
import { useEpisodeSubtitleModification } from "@/apis/hooks";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { CombinedSubtitleBadge } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { toPython } from "@/utilities";
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

interface Props {
  seriesId: number;
  episodeId: number;
  // Owning Sonarr instance for this episode (#156); scopes subtitle
  // download/delete to the correct instance on multi-instance setups.
  arrInstanceId?: number;
  missing?: boolean;
  subtitle: Subtitle;
  availableSubtitles?: Subtitle[];
}

export const Subtitle: FunctionComponent<Props> = ({
  seriesId,
  episodeId,
  arrInstanceId,
  missing = false,
  subtitle,
  availableSubtitles,
}) => {
  const navigate = useNavigate();
  const { remove, download } = useEpisodeSubtitleModification();
  const combine = useCombineSubtitles();

  const [opened, setOpen] = useState(false);
  const [compareOpened, setCompareOpened] = useState(false);

  // falsy path (null, undefined, "") means this is an embedded (in-container) subtitle track
  const isEmbedded = !subtitle.path;

  const variant: MantineColor | undefined = useMemo(() => {
    if (opened && (missing || !isEmbedded)) {
      return "highlight";
    } else if (missing) {
      return "missing";
    } else if (isEmbedded) {
      return "disabled";
    }
  }, [isEmbedded, missing, opened]);

  const selections = useMemo<FormType.ModifySubtitle[]>(() => {
    if (missing) return [];

    return [
      {
        id: episodeId,
        type: "episode",
        // Embedded track: empty path signals extraction on the backend
        path: subtitle.path ?? "",
        language: subtitle.code2,
        forced: toPython(subtitle.forced),
        hi: toPython(subtitle.hi),
        // Required by backend to identify which embedded track to extract
        from_language: isEmbedded ? subtitle.code2 : undefined,
      },
    ];
  }, [
    episodeId,
    subtitle.code2,
    subtitle.path,
    subtitle.forced,
    subtitle.hi,
    isEmbedded,
    missing,
  ]);

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

  const badgeEl = (
    <Group gap={4} wrap="nowrap">
      <Badge variant={variant} style={{ whiteSpace: "nowrap" }}>
        <Language.Text value={subtitle} long={false}></Language.Text>
      </Badge>
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
                scope: { kind: "episode", episodeId },
                body: combineRequestForSubtitle(subtitle) ?? {},
              });
            } else if (action === "view") {
              navigate(
                `/subtitles/preview/episode/${episodeId}/${encodeURIComponent(buildSubtitleLanguageKey(subtitle))}`,
              );
            } else if (action === "edit") {
              navigate(
                `/subtitles/edit/episode/${episodeId}/${encodeURIComponent(buildSubtitleLanguageKey(subtitle))}`,
              );
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
              `/subtitles/preview/episode/${episodeId}/${encodeURIComponent(buildSubtitleLanguageKey(subtitle))}`,
            );
          } else if (action === "edit") {
            navigate(
              `/subtitles/edit/episode/${episodeId}/${encodeURIComponent(buildSubtitleLanguageKey(subtitle))}`,
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
