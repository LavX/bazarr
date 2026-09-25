import { FunctionComponent } from "react";
import { Message, Number } from "@/pages/Settings/components";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";

const sizeKey = "settings-log-max_file_size_mb";
const countKey = "settings-log-backup_count";

/**
 * How much disk the log folder may use.
 *
 * The live file starts over at midnight or at the size limit, whichever comes
 * first, and only the newest rolled files are kept. The bounds match the
 * validators in the backend config, which refuse anything outside them.
 */
const LogFiles: FunctionComponent = () => {
  const size = useSettingValue<number>(sizeKey);
  const count = useSettingValue<number>(countKey);
  const ceiling = size && count ? size * (count + 1) : null;

  return (
    <>
      <Number
        label="Maximum Log File Size (MB)"
        settingKey={sizeKey}
        min={1}
        max={1024}
        allowDecimal={false}
      ></Number>
      <Message>
        The log starts a new file at midnight, or sooner once it reaches this
        size. 1 to 1024 MB, default 32
      </Message>
      <Number
        label="Log Files to Keep"
        settingKey={countKey}
        min={1}
        max={100}
        allowDecimal={false}
      ></Number>
      <Message>
        When a new file starts, older files beyond this count are deleted. 1 to
        100, default 7
      </Message>
      {ceiling !== null && (
        <Message>
          Bazarr&apos;s log files use at most about {ceiling} MB: the current
          file plus {count} older ones
        </Message>
      )}
    </>
  );
};

export default LogFiles;
