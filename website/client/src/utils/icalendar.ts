// iCalendar (.ics) export utility
// Generates RFC 5545 compliant iCalendar files for meeting events

interface Meeting {
 meeting_title: string;
 meeting_date: string;
 meeting_time?: string;
 meeting_location?: string;
 agenda_url?: string;
 minutes_url?: string;
 video_url?: string;
}

/**
 * Format date to iCal format: YYYYMMDDTHHMMSSZ
 */
function formatICalDate(dateStr: string, timeStr?: string): string {
 const date = new Date(dateStr);

 // If time is provided, parse it
 if (timeStr) {
 const timeMatch = timeStr.match(/(\d{1,2}):(\d{2})\s*(AM|PM)?/i);
 if (timeMatch) {
 let hours = parseInt(timeMatch[1]);
 const minutes = parseInt(timeMatch[2]);
 const isPM = timeMatch[3]?.toUpperCase() === "PM";

 if (isPM && hours < 12) hours += 12;
 if (!isPM && hours === 12) hours = 0;

 date.setHours(hours, minutes, 0, 0);
 }
 } else {
 // Default to 9:00 AM if no time provided
 date.setHours(9, 0, 0, 0);
 }

 // Format as YYYYMMDDTHHMMSS
 const year = date.getFullYear();
 const month = String(date.getMonth() + 1).padStart(2, "0");
 const day = String(date.getDate()).padStart(2, "0");
 const hours = String(date.getHours()).padStart(2, "0");
 const minutes = String(date.getMinutes()).padStart(2, "0");
 const seconds = String(date.getSeconds()).padStart(2, "0");

 return `${year}${month}${day}T${hours}${minutes}${seconds}`;
}

/**
 * Escape special characters for iCal format
 */
function escapeICalText(text: string): string {
 return text
 .replace(/\\/g, "\\\\")
 .replace(/;/g, "\\;")
 .replace(/,/g, "\\,")
 .replace(/\n/g, "\\n");
}

/**
 * Generate iCalendar (.ics) file content for a single meeting
 */
export function generateICalFile(meeting: Meeting, cityName: string): string {
 if (!meeting.meeting_date)
 throw new Error("Meeting date is required for calendar export");
 const startDate = formatICalDate(meeting.meeting_date, meeting.meeting_time);

 // End time is 2 hours after start by default
 const endDateTime = new Date(meeting.meeting_date);
 if (meeting.meeting_time) {
 const timeMatch = meeting.meeting_time.match(
 /(\d{1,2}):(\d{2})\s*(AM|PM)?/i
 );
 if (timeMatch) {
 let hours = parseInt(timeMatch[1]);
 const minutes = parseInt(timeMatch[2]);
 const isPM = timeMatch[3]?.toUpperCase() === "PM";

 if (isPM && hours < 12) hours += 12;
 if (!isPM && hours === 12) hours = 0;

 endDateTime.setHours(hours + 2, minutes, 0, 0);
 }
 } else {
 endDateTime.setHours(11, 0, 0, 0);
 }

 const endDate = formatICalDate(
 endDateTime.toISOString().split("T")[0],
 `${endDateTime.getHours()}:${endDateTime.getMinutes()}`
 );

 // Build description with links
 let description = `${cityName} - ${meeting.meeting_title}\\n\\n`;
 if (meeting.agenda_url) {
 description += `Agenda: ${meeting.agenda_url}\\n`;
 }
 if (meeting.minutes_url) {
 description += `Minutes: ${meeting.minutes_url}\\n`;
 }
 if (meeting.video_url) {
 description += `Video: ${meeting.video_url}\\n`;
 }

 const location = meeting.meeting_location || `${cityName} City Hall`;

 // Generate unique ID
 const uid = `${startDate}-${cityName.replace(/\s+/g, "-")}-${Date.now()}@arizona-city-council-meeting-navigator.com`;

 // Build iCal content
 const icalContent = [
 "BEGIN:VCALENDAR",
 "VERSION:2.0",
 "PRODID:-//Arizona City Council Meeting Navigator//EN",
 "CALSCALE:GREGORIAN",
 "METHOD:PUBLISH",
 "X-WR-CALNAME:Arizona City Council Meetings",
 "X-WR-TIMEZONE:America/Phoenix",
 "BEGIN:VEVENT",
 `UID:${uid}`,
 `DTSTAMP:${formatICalDate(new Date().toISOString().split("T")[0])}`,
 `DTSTART:${startDate}`,
 `DTEND:${endDate}`,
 `SUMMARY:${escapeICalText(meeting.meeting_title)} - ${cityName}`,
 `DESCRIPTION:${description}`,
 `LOCATION:${escapeICalText(location)}`,
 "STATUS:CONFIRMED",
 "SEQUENCE:0",
 "BEGIN:VALARM",
 "TRIGGER:-PT24H",
 "DESCRIPTION:Reminder: Meeting tomorrow",
 "ACTION:DISPLAY",
 "END:VALARM",
 "END:VEVENT",
 "END:VCALENDAR",
 ].join("\r\n");

 return icalContent;
}

/**
 * Download iCalendar file
 */
export function downloadICalFile(meeting: Meeting, cityName: string): void {
 if (!meeting.meeting_date) return;
 const icalContent = generateICalFile(meeting, cityName);
 const blob = new Blob([icalContent], { type: "text/calendar;charset=utf-8" });
 const link = document.createElement("a");
 link.href = URL.createObjectURL(blob);

 const date = new Date(meeting.meeting_date);
 const dateStr = isNaN(date.getTime())
 ? "unknown-date"
 : date.toISOString().split("T")[0];
 const filename = `${cityName.replace(/\s+/g, "-")}-${dateStr}-meeting.ics`;

 link.download = filename;
 document.body.appendChild(link);
 link.click();
 document.body.removeChild(link);
 URL.revokeObjectURL(link.href);
}

/**
 * Generate Google Calendar URL
 */
export function getGoogleCalendarUrl(
 meeting: Meeting,
 cityName: string
): string {
 if (!meeting.meeting_date) return "#";
 const startDate = new Date(meeting.meeting_date);
 if (isNaN(startDate.getTime())) return "#";

 // Parse time if provided
 if (meeting.meeting_time) {
 const timeMatch = meeting.meeting_time.match(
 /(\d{1,2}):(\d{2})\s*(AM|PM)?/i
 );
 if (timeMatch) {
 let hours = parseInt(timeMatch[1]);
 const minutes = parseInt(timeMatch[2]);
 const isPM = timeMatch[3]?.toUpperCase() === "PM";

 if (isPM && hours < 12) hours += 12;
 if (!isPM && hours === 12) hours = 0;

 startDate.setHours(hours, minutes, 0, 0);
 }
 } else {
 startDate.setHours(9, 0, 0, 0);
 }

 // End time is 2 hours later
 const endDate = new Date(startDate);
 endDate.setHours(endDate.getHours() + 2);

 // Format dates for Google Calendar (YYYYMMDDTHHMMSS)
 const formatGoogleDate = (date: Date) => {
 const year = date.getFullYear();
 const month = String(date.getMonth() + 1).padStart(2, "0");
 const day = String(date.getDate()).padStart(2, "0");
 const hours = String(date.getHours()).padStart(2, "0");
 const minutes = String(date.getMinutes()).padStart(2, "0");
 const seconds = String(date.getSeconds()).padStart(2, "0");
 return `${year}${month}${day}T${hours}${minutes}${seconds}`;
 };

 const dates = `${formatGoogleDate(startDate)}/${formatGoogleDate(endDate)}`;
 const title = encodeURIComponent(`${meeting.meeting_title} - ${cityName}`);
 const location = encodeURIComponent(
 meeting.meeting_location || `${cityName} City Hall`
 );

 let details = `${cityName} - ${meeting.meeting_title}\n\n`;
 if (meeting.agenda_url) details += `Agenda: ${meeting.agenda_url}\n`;
 if (meeting.minutes_url) details += `Minutes: ${meeting.minutes_url}\n`;
 if (meeting.video_url) details += `Video: ${meeting.video_url}\n`;
 const encodedDetails = encodeURIComponent(details);

 return `https://calendar.google.com/calendar/render?action=TEMPLATE&text=${title}&dates=${dates}&details=${encodedDetails}&location=${location}&ctz=America/Phoenix`;
}
