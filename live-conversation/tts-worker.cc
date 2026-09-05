#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#include "sherpa-onnx/c-api/cxx-api.h"

namespace {
constexpr int32_t kOutputSampleRate = 24000;
constexpr uint32_t kMaxRequestBytes = 1024 * 1024;

bool ReadExact(void *data, size_t size) {
  auto *output = static_cast<char *>(data);
  size_t offset = 0;
  while (offset < size) {
    std::cin.read(output + offset, static_cast<std::streamsize>(size - offset));
    const auto count = static_cast<size_t>(std::cin.gcount());
    if (count == 0) return false;
    offset += count;
  }
  return true;
}

void WriteUint32(uint32_t value) {
  const char bytes[] = {static_cast<char>(value & 0xff), static_cast<char>((value >> 8) & 0xff),
                        static_cast<char>((value >> 16) & 0xff), static_cast<char>((value >> 24) & 0xff)};
  std::cout.write(bytes, sizeof(bytes));
}

uint32_t ReadUint32(const char bytes[4]) {
  return static_cast<uint32_t>(static_cast<unsigned char>(bytes[0])) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[1])) << 8) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[2])) << 16) |
         (static_cast<uint32_t>(static_cast<unsigned char>(bytes[3])) << 24);
}

std::vector<int16_t> ToPcm16(const std::vector<float> &samples) {
  std::vector<int16_t> output(samples.size());
  std::transform(samples.begin(), samples.end(), output.begin(), [](float sample) {
    const float clipped = std::clamp(sample, -1.0f, 1.0f);
    return static_cast<int16_t>(std::lrint(clipped * (clipped < 0 ? 32768.0f : 32767.0f)));
  });
  return output;
}

std::string ArgValue(int argc, char **argv, const std::string &name) {
  for (int i = 1; i + 1 < argc; ++i) {
    if (argv[i] == name) return argv[i + 1];
  }
  return {};
}

void WriteResponse(uint32_t status, const void *data, size_t size) {
  WriteUint32(status);
  WriteUint32(static_cast<uint32_t>(size));
  if (size > 0) std::cout.write(static_cast<const char *>(data), static_cast<std::streamsize>(size));
  std::cout.flush();
}
}  // namespace

int main(int argc, char **argv) {
  const std::string model = ArgValue(argc, argv, "--model");
  const std::string tokens = ArgValue(argc, argv, "--tokens");
  const std::string data_dir = ArgValue(argc, argv, "--data-dir");
  const std::string voices = ArgValue(argc, argv, "--voices");
  const std::string threads_text = ArgValue(argc, argv, "--threads");
  const std::string speed_text = ArgValue(argc, argv, "--speed");
  const std::string sid_text = ArgValue(argc, argv, "--sid");
  if (model.empty() || tokens.empty() || data_dir.empty() || voices.empty()) {
    std::cerr << "Required arguments: --model, --tokens, --data-dir, --voices\n";
    return 2;
  }

  try {
    sherpa_onnx::cxx::OfflineTtsConfig config;
    config.model.kokoro.model = model;
    config.model.kokoro.voices = voices;
    config.model.kokoro.tokens = tokens;
    config.model.kokoro.data_dir = data_dir;
    config.model.num_threads = threads_text.empty() ? 8 : std::stoi(threads_text);
    config.model.provider = "cpu";
    const float speed = speed_text.empty() ? 1.0f : std::stof(speed_text);
    const int32_t sid = sid_text.empty() ? 9 : std::stoi(sid_text);
    auto tts = sherpa_onnx::cxx::OfflineTts::Create(config);

    std::cout.write("RTV1", 4);
    WriteUint32(kOutputSampleRate);
    std::cout.flush();
    while (true) {
      char length_bytes[4];
      if (!ReadExact(length_bytes, sizeof(length_bytes))) break;
      const uint32_t length = ReadUint32(length_bytes);
      if (length == 0 || length > kMaxRequestBytes) {
        const std::string error = "Invalid TTS request length";
        WriteResponse(1, error.data(), error.size());
        continue;
      }
      std::string text(length, '\0');
      if (!ReadExact(text.data(), text.size())) break;
      try {
        auto generated = tts.Generate(text, sid, speed);
        auto pcm = ToPcm16(generated.samples);
        WriteResponse(0, pcm.data(), pcm.size() * sizeof(int16_t));
      } catch (const std::exception &error) {
        const std::string message = error.what();
        WriteResponse(1, message.data(), message.size());
      }
    }
  } catch (const std::exception &error) {
    std::cerr << "TTS worker failed: " << error.what() << '\n';
    return 1;
  }
  return 0;
}
