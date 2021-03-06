#pragma once

#include <cstring>
#include <string>
#include <map>
#include <vector>
#include <algorithm>
#include <regex>
#include <sstream>

class StringIndex {
  private:
    std::vector<std::string> strings;
    std::vector<int64_t> indices;

  public: 
    size_t size() const;
    StringIndex(const std::map<std::string, int64_t>& mapping);
    StringIndex();
    int64_t string_lookup(const std::string& s) const;
};


StringIndex build_string_index(const std::string& indexfn);

namespace QueryUtils {

  template <typename Iter, typename T>
    Iter binary_search(Iter begin, Iter end, const T& key) {
      auto i = std::lower_bound(begin, end, key);
      
      if (i != end && (key == *i)) {
        return i; 
      } else {
        return end; 
      }
  }
}



// for array based strings
#include <array>
#include <cassert>
#define MAX_STR_LEN 28

#include <iostream>
template<size_t N, class Iterable, bool truncate=false>
std::array<char, N> to_array(const Iterable& x) {
  if (!truncate) {
    assert(x.size() <= N-1);
    std::array<char, N> d;
    std::copy(x.begin(), x.end(), d.data());
    *(d.data()+x.size()) = '\0'; // copy null terminator
    
    // ensure normalization of std::arrays that are equal strings
    if (x.size()+1 < N) {
      std::memset(d.data() + x.size() + 1, 0, N - x.size());
    }

    return d;
  } else {
    std::array<char, N> d;
    uint64_t item = 0;

    // only copy up to N-1 elements
    std::copy_if(x.begin(), x.end(), d.data(), [&item](const char& c) {
        return item++ < N-1;
        });

    *(d.data() + std::min(x.size(), N-1)) = '\0';

    // ensure normalization of std::arrays that are equal strings
    if (x.size()+1 < N) {
      std::memset(d.data() + x.size() + 1, 0, N - x.size());
    }

    return d;
  }
}

// utility to see full content of char array 
template <size_t N>
void dumps(std::ostream& o, const std::array<char, N>& arr) {
  o << "[";
  for (int i=0; i<N; i++) {
    o << arr[i] << "|";
  }
  o << "]";
}

template <size_t N>
std::ostream& operator<<(std::ostream& o, const std::array<char, N>& arr) {
  // copy to a string so null terminator is used  
  std::string s(arr.data());
  o << s;
  return o;
}

template <size_t N>
bool operator==(const std::array<char, N>& arr, const std::string& str) {
  return std::string(arr.data()) == str;
}

template <size_t N>
bool operator==(const std::string& str, const std::array<char, N>& arr) {
  return arr == str;
}

template <size_t N>
bool operator!=(const std::array<char, N>& arr, const std::string& str) {
  return std::string(arr.data()) != str;
}

template <size_t N>
bool operator!=(const std::string& str, const std::array<char, N>& arr) {
  return arr != str;
}


// character arrays to be compared using string comparison semantics
// rather than character-for-character equivalence
// IMPORTANT: operator== for std::array does not always resolve
//            to this function, e.g., when comparing std::tuples of std::arrays
//            the built-in std::array operator== is used.
//            So we are not currently relying on this == for Pred of unordered_map on tuples of arrays
template <size_t N>
bool operator==(const std::array<char, N>& arr1, const std::array<char, N>& arr2) {
  return std::string(arr1.data()) == std::string(arr2.data());
}

template <size_t N>
bool operator!=(const std::array<char, N>& arr1, const std::array<char, N>& arr2) {
  return std::string(arr1.data()) != std::string(arr2.data());
}

// TODO see issue #434 Use C++ implicit type conversions for std::array and std::string
template <size_t N>
bool operator<=(const std::array<char, N>& lhs, const std::string& rhs) {
  return std::string(lhs.data()) <= rhs; 
}

template <size_t N>
bool operator<(const std::array<char, N>& lhs, const std::string& rhs) {
  return std::string(lhs.data()) < rhs; 
}

template <size_t N>
bool operator>=(const std::array<char, N>& lhs, const std::string& rhs) {
  return std::string(lhs.data()) >= rhs; 
}

template <size_t N>
bool operator>(const std::array<char, N>& lhs, const std::string& rhs) {
  return std::string(lhs.data()) > rhs; 
}

std::regex compile_like_pattern(const std::string& pattern);

template <size_t N>
bool operator%(const std::array<char, N>& s, std::regex r) {
  return std::regex_match(std::string(s.data()), r);
}

template <size_t N>
std::string substr(const std::array<char, N>& s, uint64_t pos, uint64_t len) {
  return std::string(s.data()).substr(pos, len);
}
