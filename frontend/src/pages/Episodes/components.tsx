import { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  Badge,
  Group,
  MantineColor,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useEpisodeSubtitleModification } from "@/apis/hooks";
import Language from "@/components/bazarr/Language";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { toPython } from "@/utilities";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  getSyncEngineLabel,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputSubtitle,
  sortSyncOutputSubtitles,
} from "@/utilities/subtitles";

interface Props {
  seriesId: number;
  episodeId: number;
  missing?: boolean;
  subtitle: Subtitle;
  availableSubtitles?: Subtitle[];
}

export const Subtitle: FunctionComponent<Props> = ({
  seriesId,
  episodeId,
  missing = false,
  subtitle,
  availableSubtitles,
}) => {
  const navigate = useNavigate();
  const { remove, download } = useEpisodeSubtitleModification();

  const [opened, setOpen] = useState(false);
  const [compareOpened, setCompareOpened] = useState(false);

  const disabled = subtitle.path === null;

  const variant: MantineColor | undefined = useMemo(() => {
    if (opened && (missing || !disabled)) {
      return "highlight";
    } else if (missing) {
      return "missing";
    } else if (disabled) {
      return "disabled";
    }
  }, [disabled, missing, opened]);

  const badgeTooltip = useMemo(() => {
    if (missing) return "Missing subtitle";
    if (disabled) return "Embedded subtitle";
    return "Available subtitle";
  }, [missing, disabled]);

  const selections = useMemo<FormType.ModifySubtitle[]>(() => {
    const list: FormType.ModifySubtitle[] = [];

    if (subtitle.path) {
      list.push({
        id: episodeId,
        type: "episode",
        language: subtitle.code2,
        path: subtitle.path,
        forced: toPython(subtitle.forced),
        hi: toPython(subtitle.hi),
      });
    }

    return list;
  }, [episodeId, subtitle.code2, subtitle.path, subtitle.forced, subtitle.hi]);

  // For missing subs: translation sources from available subtitles
  const translationSources = useMemo(
    () =>
      (availableSubtitles ?? []).filter(
        (s) => s.path && !isSyncOutputSubtitle(s),
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
    !disabled &&
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

  if (disabled && !missing) {
    return (
      <Tooltip.Floating label={badgeTooltip}>
        <UnstyledButton
          aria-label={`${subtitle.name || subtitle.code2} (embedded)`}
          tabIndex={-1}
        >
          {badgeEl}
        </UnstyledButton>
      </Tooltip.Floating>
    );
  }

  // Interactive badges: no Tooltip wrapper, as it breaks Menu.Target click handling
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
        canSync={canSynchronizeSubtitle(subtitle)}
        missingLanguage={missing ? subtitle : undefined}
        translationSources={missing ? translationSources : undefined}
        canCompareSyncOutputs={canCompareSyncOutputs}
        mediaId={episodeId}
        mediaType="episode"
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
