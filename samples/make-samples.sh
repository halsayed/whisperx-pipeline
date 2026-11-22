#!/bin/sh
# Generates the two synthetic test recordings used in the README test:
#   support-call-en.mp3  English, two voices
#   support-call-ar.wav  Arabic, one voice plus the same voice pitch-shifted
# Needs macOS (`say`, voices Samantha, Daniel, Majed) and ffmpeg.
set -e
out=${1:-.}
tmp=$(mktemp -d)
cd "$tmp"

i=0
line() { i=$((i + 1)); say -v "$1" -o "$2$i.aiff" "$3"; }

line Samantha e "Good morning, and thank you for calling the support desk. My name is Sarah. How can I help you today?"
line Daniel   e "Hello Sarah. I am having trouble with my storage cluster. One of the nodes went offline last night at around eleven thirty."
line Samantha e "I am sorry to hear that. Can you tell me the model of the node, and whether the other three nodes are still healthy?"
line Daniel   e "Yes, the other three nodes are healthy. The failed node is the fourth one in the block, and the power light is blinking amber."
line Samantha e "Thank you. I will open a case and send a field engineer tomorrow morning. Your case number is four five seven two one."
line Daniel   e "That is great. Thank you very much for your help."
i=0
line Majed a "صباح الخير، شكراً لاتصالك بمركز الدعم الفني. اسمي أحمد. كيف يمكنني مساعدتك اليوم؟"
line Majed a "مرحباً أحمد. لدي مشكلة في نظام التخزين. توقف أحد الخوادم عن العمل الليلة الماضية في الساعة الحادية عشرة والنصف."
line Majed a "أنا آسف لسماع ذلك. هل يمكنك أن تخبرني برقم الخادم، وهل الخوادم الثلاثة الأخرى تعمل بشكل طبيعي؟"
line Majed a "نعم، الخوادم الأخرى تعمل بشكل طبيعي. الخادم المتعطل هو الرابع، وضوء الطاقة يومض باللون الأصفر."
line Majed a "شكراً لك. سأفتح بلاغاً وأرسل مهندساً ميدانياً غداً صباحاً."
line Majed a "ممتاز. شكراً جزيلاً على مساعدتك."

pad="apad=pad_dur=0.5"
shift_up="asetrate=22050*1.25,aresample=22050,atempo=0.8,$pad"   # second Arabic "speaker"
for n in 1 2 3 4 5 6; do
  ffmpeg -loglevel error -y -i e$n.aiff -af "$pad" -ar 16000 -ac 1 e$n.wav
  if [ $((n % 2)) -eq 0 ]; then f=$shift_up; else f=$pad; fi
  ffmpeg -loglevel error -y -i a$n.aiff -af "$f" -ar 16000 -ac 1 a$n.wav
  echo "file 'e$n.wav'" >> en.txt
  echo "file 'a$n.wav'" >> ar.txt
done
cd - >/dev/null
ffmpeg -loglevel error -y -f concat -safe 0 -i "$tmp/en.txt" -c:a libmp3lame -q:a 4 "$out/support-call-en.mp3"
ffmpeg -loglevel error -y -f concat -safe 0 -i "$tmp/ar.txt" "$out/support-call-ar.wav"
rm -rf "$tmp"
echo "wrote $out/support-call-en.mp3 and $out/support-call-ar.wav"
