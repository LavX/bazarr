import { NotificationData } from "@mantine/notifications";

export const notification = {
  info: (title: string, message: string): NotificationData => {
    return {
      title,
      message,
      autoClose: 5 * 1000,
    };
  },

  warn: (title: string, message: string): NotificationData => {
    return {
      title,
      message,
      color: "yellow",
      autoClose: 6 * 1000,
    };
  },

  error: (title: string, message: string): NotificationData => {
    return {
      title,
      message,
      color: "red",
      autoClose: 7 * 1000,
    };
  },
};
