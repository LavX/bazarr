import { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, MantineColor, Tooltip, UnstyledButton } from "@mantine/core";
import { useEpisodeSubtitleModification } from "@/apis/hooks";
import Language from "@/components/bazarr/Language";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { toPython } from "@/utilities";

function buildLanguageKey(sub: Subtitle): string {
  let key = sub.code2;
  if (sub.hi) key += ":hi";
  if (sub.forced) key += ":forced";
  return key;
}

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
    () => (availableSubtitles ?? []).filter((s) => s.path),
    [availableSubtitles],
  );

  const badgeEl = (
    <Badge variant={variant}>
      <Language.Text value={subtitle} long={false}></Language.Text>
    </Badge>
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
    <SubtitleToolsMenu
      menu={{
        trigger: "click",
        onOpen: () => setOpen(true),
        onClose: () => setOpen(false),
      }}
      selections={selections}
      missingLanguage={missing ? subtitle : undefined}
      translationSources={missing ? translationSources : undefined}
      mediaId={episodeId}
      mediaType="episode"
      onAction={async (action) => {
        if (action === "view") {
          navigate(
            `/subtitles/preview/episode/${episodeId}/${encodeURIComponent(buildLanguageKey(subtitle))}`,
          );
        } else if (action === "edit") {
          navigate(
            `/subtitles/edit/episode/${episodeId}/${encodeURIComponent(buildLanguageKey(subtitle))}`,
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
  );
};
